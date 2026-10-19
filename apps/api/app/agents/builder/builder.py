"""Builder: a container image for the app Scout described, built and smoke-tested in the sandbox.

    outcome = await Builder(llm, sandbox).build(root, spec, app=..., tag=..., run_id=..., ...)
    smoke = await builder.smoke(outcome, spec, env, run_id=...)

The loop (docs/prompts/phase4-rizwan.md, M4):

- The repo's own Dockerfile, if there is one, is built as is first. Otherwise write_dockerfile
  (Super, reasoning low) writes one.
- A failed build sends the failing step, the error lines, and the end of the log to fix_build
  (Super, reasoning on; escalated one tier from attempt 4: Ultra, or Super while
  DEV_MODE_NO_ULTRA), which returns a whole corrected Dockerfile. At most 5 attempts.
- The same error after the same kind of fix, or a fix that changes nothing, stops with
  stop_reason no_progress.
- Network failures (registry or package index errors, timeouts, 5xx) are retried up to 2 times
  with backoff, without a model call and without using an attempt.
- Every attempt is reported (`BuildAttempt`: diff, error, model badge, tokens, cost) through
  `on_attempt`. Builder writes only inside the materialized repo copy (`Dockerfile.groundcrew`).
- Nothing is pushed here: the push is the first step of apply, after approval.
"""

import asyncio
import difflib
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from app.agents.builder.prompts import fix_messages, write_messages
from app.agents.builder.schemas import DockerfileDraft, DockerfileFix
from app.agents.scout.appspec import AppSpec
from app.llm.budget import RunBudget
from app.llm.client import ChatModel, LLMError, LLMResult
from app.llm.router import Task
from app.pipeline.events import (
    LOG_TAIL_CHARS,
    MAX_BUILD_ATTEMPTS,
    AttemptModelCall,
    BuildAttempt,
)
from app.sandbox.base import BuildResult, Sandbox, SmokeEnv, SmokeResult
from app.sandbox.logs import tail

GENERATED_DOCKERFILE = "Dockerfile.groundcrew"
# Fixes that produce attempt 4 and 5 go one tier up (Builder's escalation).
ESCALATE_FROM_ATTEMPT = 4
TRANSIENT_RETRIES = 2
RETRY_DELAYS_SECONDS = (10.0, 30.0)
DEFAULT_PORTS = {
    "python": 8000,
    "javascript": 3000,
    "typescript": 3000,
    "go": 8080,
    "java": 8080,
    "ruby": 3000,
    "rust": 8080,
    # The php:<v>-apache image the prompt asks for serves on 80.
    "php": 80,
    # .NET 8 images listen on 8080 (ASPNETCORE_HTTP_PORTS).
    "dotnet": 8080,
}
# Where a framework's convention differs from its language's (Scout usually sets the port).
FRAMEWORK_PORTS = {
    "spring": 8080,
    "quarkus": 8080,
    "rails": 3000,
    "sinatra": 4567,
    "rack": 9292,
    "axum": 3000,
    "actix": 8080,
    "rocket": 8000,
    "laravel": 80,
    "slim": 80,
    "aspnet": 8080,
}

# Network trouble in a build log: the build did not fail because of the Dockerfile.
_TRANSIENT = re.compile(
    r"(?i)(temporary failure in name resolution|could not resolve host|name or service not "
    r"known|connection (reset|refused|broken|timed out|aborted)|broken pipe|read timed out|"
    r"readtimeouterror|tls handshake timeout|i/o timeout|net/http: request canceled|"
    r"unexpected eof|toomanyrequests|429 too many requests|50[0234] (internal server error|bad "
    r"gateway|service unavailable|gateway timeout)|failed to fetch anonymous token|"
    r"failed to do request|network is unreachable|max retries exceeded)"
)
_DIGITS = re.compile(r"\d+")
_HEX = re.compile(r"\b[0-9a-f]{7,}\b")


class BuilderError(RuntimeError):
    """Builder could not go on (a model call failed). The message is safe to show."""


class CallRecorder(Protocol):
    """The on_call hook that records each model call as a run step; `last_seq` is that step's
    seq, so an attempt card can point at its model call."""

    last_seq: int | None

    async def __call__(self, result: LLMResult) -> None: ...


@dataclass
class BuildOutcome:
    ok: bool
    image_ref: str | None
    image_id: str | None
    dockerfile: str
    attempts: list[BuildAttempt] = field(default_factory=list)
    # A build passed after Builder changed the Dockerfile because of a failure.
    automatic_fix: bool = False
    stop_reason: Literal["no_progress", "max_attempts"] | None = None
    error: str | None = None
    port: int = 8000


def app_port(spec: AppSpec, dockerfile: str | None = None) -> int:
    if spec.port:
        return spec.port
    if dockerfile and (found := re.search(r"(?im)^\s*EXPOSE\s+(\d+)", dockerfile)):
        return int(found.group(1))
    if spec.framework in FRAMEWORK_PORTS:
        return FRAMEWORK_PORTS[spec.framework]
    return DEFAULT_PORTS.get(str(spec.language or ""), 8080)


def is_transient(result: BuildResult) -> bool:
    """A network or registry failure, or a timeout: retry without a fix."""
    if result.ok:
        return False
    if result.timed_out:
        return True
    text = "\n".join(result.error_lines) + "\n" + result.log_tail
    return bool(_TRANSIENT.search(text))


# Lines that say a step failed but not why (pip's and BuildKit's wrappers).
_GENERIC = re.compile(
    r"(?i)(did not complete successfully|finished with status 'error'|"
    r"subprocess-exited-with-error|^[│|]?\s*exit code: \d+$|^error: exception:?$)"
)
# Lines that name a cause: "Error: pg_config executable not found.", "ModuleNotFoundError: x".
_CAUSE = re.compile(r"(?i)(\w*(error|exception)\s*:\s*\S|not found|no such file|no matching)")


_FENCE = re.compile(r"^\s*```[\w-]*\s*\n|\n\s*```\s*$")


def clean_dockerfile(text: str) -> str:
    """The Dockerfile as the model meant it: no Markdown fences, real newlines (some replies
    put the whole file on one line with literal \\n), and a trailing newline."""
    text = text.strip()
    if "\n" not in text and "\\n" in text:
        text = text.replace("\\n", "\n").replace('\\"', '"')
    text = _FENCE.sub("", text).strip()
    return text + "\n"


def error_summary(result: BuildResult) -> str | None:
    """The line that names the cause, for the collapsed attempt card."""
    specific = [line for line in result.error_lines if not _GENERIC.search(line.strip())]
    causes = [line for line in specific if _CAUSE.search(line)]
    candidates = causes or specific or result.error_lines
    return candidates[0].strip()[:200] if candidates else None


def signature(result: BuildResult) -> str:
    """What "the same error" means: the failing step and the first error line, without
    numbers and hashes (timestamps, line numbers, and layer IDs change between runs)."""
    text = f"{result.failing_step or ''}|{error_summary(result) or ''}".lower()
    return _DIGITS.sub("#", _HEX.sub("h", text))


def unified_diff(old: str | None, new: str, attempt: int) -> str:
    if old is None:
        return new
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        f"Dockerfile (attempt {attempt - 1})",
        f"Dockerfile (attempt {attempt})",
    )
    return "".join(lines)


def _model_call(
    result: LLMResult, recorder: CallRecorder, task: Literal["write_dockerfile", "fix_build"]
) -> AttemptModelCall:
    return AttemptModelCall(
        task=task,
        model_tier=str(result.tier or ""),
        model_id=result.model_id,
        reasoning_mode=str(result.reasoning_mode or ""),
        escalated=result.escalated,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        cost_usd=float(result.cost_usd) if result.cost_usd is not None else None,
        step_seq=recorder.last_seq or 0,
    )


class Builder:
    def __init__(
        self,
        llm: ChatModel,
        sandbox: Sandbox,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_delays: Sequence[float] = RETRY_DELAYS_SECONDS,
        max_attempts: int = MAX_BUILD_ATTEMPTS,
    ) -> None:
        self.llm = llm
        self.sandbox = sandbox
        self._sleep = sleep
        self.retry_delays = retry_delays
        self.max_attempts = max_attempts

    def _write(self, root: Path, dockerfile: str) -> Path:
        """Builder writes only inside the materialized repo copy."""
        path = (root / GENERATED_DOCKERFILE).resolve()
        if not path.is_relative_to(root.resolve()):
            raise BuilderError("Refusing to write outside the repository copy.")
        path.write_text(dockerfile if dockerfile.endswith("\n") else dockerfile + "\n")
        return path

    async def _ask(
        self,
        task: Task,
        messages: list[dict[str, str]],
        schema: type[DockerfileDraft] | type[DockerfileFix],
        *,
        budget: RunBudget,
        recorder: CallRecorder,
        escalate: bool = False,
    ) -> LLMResult:
        try:
            return await self.llm.chat(
                task, messages, budget=budget, schema=schema, escalate=escalate, on_call=recorder
            )
        except LLMError as exc:
            raise BuilderError(f"{task} failed: {exc}") from None

    async def _build_once(
        self,
        root: Path,
        dockerfile: Path,
        tag: str,
        run_id: str,
        on_note: Callable[[str], Awaitable[None]],
    ) -> BuildResult:
        """One build, retried on network trouble without counting an attempt."""
        result = await self.sandbox.build(root, dockerfile, tag, run_id=run_id)
        for retry in range(TRANSIENT_RETRIES):
            if not is_transient(result):
                break
            delay = self.retry_delays[min(retry, len(self.retry_delays) - 1)]
            cause = "a timeout" if result.timed_out else "a network error"
            await on_note(
                f"Retrying after {cause} ({retry + 1} of {TRANSIENT_RETRIES}, in {delay:.0f} s): "
                f"{error_summary(result) or 'the build was interrupted'}"
            )
            await self._sleep(delay)
            result = await self.sandbox.build(root, dockerfile, tag, run_id=run_id)
        return result

    async def build(
        self,
        root: Path,
        spec: AppSpec,
        *,
        app: str,
        tag: str,
        run_id: str,
        budget: RunBudget,
        recorder: CallRecorder,
        on_attempt: Callable[[BuildAttempt], Awaitable[None]],
        on_note: Callable[[str], Awaitable[None]],
    ) -> BuildOutcome:
        repo_dockerfile = root / "Dockerfile"
        use_repo = spec.has_dockerfile and repo_dockerfile.is_file()
        source: Literal["repo", "generated", "fixed"]
        model_call: AttemptModelCall | None = None
        rationale: str | None = None
        if use_repo:
            source, current = "repo", repo_dockerfile.read_text(errors="replace")
            path = repo_dockerfile
        else:
            port = app_port(spec)
            result = await self._ask(
                Task.write_dockerfile,
                write_messages(root, spec, port),
                DockerfileDraft,
                budget=budget,
                recorder=recorder,
            )
            draft: DockerfileDraft = result.parsed
            source, current, rationale = (
                "generated",
                clean_dockerfile(draft.dockerfile),
                draft.rationale,
            )
            model_call = _model_call(result, recorder, "write_dockerfile")
            path = self._write(root, current)

        attempts: list[BuildAttempt] = []
        signatures: list[str] = []
        fix_kinds: list[str | None] = [None]
        previous: str | None = None
        fixed = False
        earlier: list[str] = []
        for number in range(1, self.max_attempts + 1):
            started = time.monotonic()
            built = await self._build_once(
                root, path, f"gc-build/{app}:{tag}-a{number}", run_id, on_note
            )
            attempt = BuildAttempt(
                attempt=number,
                max_attempts=self.max_attempts,
                dockerfile_source=source,
                dockerfile_diff=unified_diff(previous, current, number),
                rationale=rationale,
                ok=built.ok,
                duration_ms=built.duration_ms or int((time.monotonic() - started) * 1000),
                failing_step=None if built.ok else built.failing_step,
                error_summary=None if built.ok else error_summary(built),
                log_tail=None if built.ok else tail(built.log_tail, LOG_TAIL_CHARS),
                image_ref=built.image_ref if built.ok else None,
                model_call=model_call,
            )
            attempts.append(attempt)
            await on_attempt(attempt)
            if built.ok and built.image_ref:
                return BuildOutcome(
                    ok=True,
                    image_ref=built.image_ref,
                    image_id=await self.sandbox.image_id(built.image_ref),
                    dockerfile=current,
                    attempts=attempts,
                    automatic_fix=fixed,
                    port=app_port(spec, current),
                )

            signatures.append(signature(built))
            earlier.append(
                f"attempt {number} ({fix_kinds[-1] or source}): failed at "
                f"{built.failing_step or 'unknown step'}: {error_summary(built) or 'no error line'}"
            )
            if (
                len(signatures) >= 3
                and signatures[-1] == signatures[-2]
                and fix_kinds[-1] is not None
                and fix_kinds[-1] == fix_kinds[-2]
            ):
                return self._stopped(
                    attempts,
                    current,
                    "no_progress",
                    spec,
                    f"The same error after the same kind of fix ({fix_kinds[-1]}): "
                    f"{error_summary(built) or built.failing_step}",
                )
            if number == self.max_attempts:
                return self._stopped(
                    attempts,
                    current,
                    "max_attempts",
                    spec,
                    f"The build still fails after {number} attempts: "
                    f"{error_summary(built) or built.failing_step}",
                )

            result = await self._ask(
                Task.fix_build,
                fix_messages(
                    root,
                    spec,
                    app_port(spec, current),
                    current,
                    built.failing_step,
                    built.error_lines,
                    built.log_tail,
                    earlier,
                ),
                DockerfileFix,
                budget=budget,
                recorder=recorder,
                escalate=number + 1 >= ESCALATE_FROM_ATTEMPT,
            )
            fix: DockerfileFix = result.parsed
            fixed_text = clean_dockerfile(fix.dockerfile)
            if fixed_text.strip() == current.strip():
                return self._stopped(
                    attempts, current, "no_progress", spec, "The fix returned the same Dockerfile."
                )
            previous, current = current, fixed_text
            source, rationale, fixed = "fixed", fix.rationale, True
            fix_kinds.append(fix.fix_kind)
            model_call = _model_call(result, recorder, "fix_build")
            path = self._write(root, current)
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _stopped(
        attempts: list[BuildAttempt],
        dockerfile: str,
        reason: Literal["no_progress", "max_attempts"],
        spec: AppSpec,
        error: str,
    ) -> BuildOutcome:
        return BuildOutcome(
            ok=False,
            image_ref=None,
            image_id=None,
            dockerfile=dockerfile,
            attempts=attempts,
            stop_reason=reason,
            error=error,
            port=app_port(spec, dockerfile),
        )

    async def smoke(
        self, outcome: BuildOutcome, spec: AppSpec, env: list[SmokeEnv], *, run_id: str
    ) -> SmokeResult:
        """Run the built image with the deploy's env and probe it. With a health path, that
        path decides; "/" is only recorded."""
        if not outcome.ok or not outcome.image_ref:
            raise BuilderError("Nothing to smoke-test: the build did not pass.")
        return await self.sandbox.run_smoke(
            outcome.image_ref, outcome.port, env, run_id=run_id, health_path=spec.health_path
        )
