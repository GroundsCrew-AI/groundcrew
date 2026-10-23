"""Builder on FakeLLM and a scripted sandbox: fixes, escalation, no_progress, the attempt limit,
network retries that do not count, and what each attempt reports."""

import json
from pathlib import Path
from typing import Any

import pytest

from app.agents.builder import Builder, BuilderError, is_transient
from app.agents.builder.builder import GENERATED_DOCKERFILE, SMOKE_STEP, signature
from app.agents.scout.appspec import AppSpec, EnvVar
from app.config import Settings
from app.llm.budget import MemoryUsageStore, RunBudget
from app.llm.client import LLMError, LLMResult
from app.pipeline.events import BuildAttempt
from app.sandbox.base import (
    BuildResult,
    PushResult,
    PushTarget,
    SmokeCheck,
    SmokeEnv,
    SmokeResult,
    daemon_unreachable,
    unavailable_text,
)
from app.sandbox.logs import error_lines, failing_step
from tests.fakes import FakeLLM, reply

pytestmark = pytest.mark.anyio

LOGS = Path(__file__).parent / "fixtures" / "sandbox"
PSYCOPG2 = (LOGS / "psycopg2-build.log").read_text()
FLAKE = (LOGS / "pip-network-flake.log").read_text()
NAIVE = "FROM python:3.12-slim\nRUN pip install -r requirements.txt\nCMD gunicorn app:app\n"
FIXED = (
    "FROM python:3.12-slim\nRUN apt-get install -y gcc libpq-dev\n"
    "RUN pip install -r requirements.txt\nCMD gunicorn app:app\n"
)


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "model_nano": "m/nano",
        "model_super": "m/super",
        "model_ultra": "m/ultra",
        "dev_mode_no_ultra": False,
        **overrides,
    }
    return Settings(_env_file=None, **values)


def _budget(settings: Settings) -> RunBudget:
    return RunBudget(usage=MemoryUsageStore(), settings=settings)


def failed(log: str, *, timed_out: bool = False) -> BuildResult:
    return BuildResult(
        ok=False,
        log_tail=log,
        image_ref=None,
        duration_ms=1000,
        failing_step=failing_step(log),
        error_lines=error_lines(log),
        timed_out=timed_out,
    )


def other_error(n: int) -> BuildResult:
    log = (
        f"#9 [4/5] RUN step{n}\n#9 1.0 Error: thing {n} is missing\n"
        f'#9 ERROR: process "/bin/sh -c step{n}" did not complete successfully: exit code: 1\n'
    )
    return failed(log)


class ScriptedSandbox:
    def __init__(self, *results: BuildResult) -> None:
        self.results = list(results)
        self.builds: list[tuple[Path, str]] = []
        self.pushed: list[str] = []

    async def build(
        self, context_dir: Path, dockerfile: Path, tag: str, *, run_id: str
    ) -> BuildResult:
        self.builds.append((dockerfile, dockerfile.read_text()))
        result = self.results.pop(0)
        if result.ok:
            return BuildResult(ok=True, log_tail="", image_ref=tag, duration_ms=2000)
        return result

    async def run_smoke(
        self,
        image_ref: str,
        port: int,
        env: list[SmokeEnv],
        *,
        run_id: str,
        health_path: str | None = None,
    ) -> SmokeResult:
        return SmokeResult(
            ok=True, checks=[SmokeCheck(health_path or "/", True, 200, "HTTP 200")], duration_ms=5
        )

    async def push(self, image_ref: str, target: PushTarget) -> PushResult:
        self.pushed.append(image_ref)
        raise AssertionError("Builder never pushes")

    async def push_to_groundcrew(
        self, image_ref: str, registry_url: str, repository: str, tag: str
    ) -> PushResult:
        raise AssertionError("Builder never pushes")

    async def image_id(self, image_ref: str) -> str:
        return "sha256:" + "c" * 64

    async def cleanup(self, run_id: str) -> None:
        return None


OK = BuildResult(ok=True, log_tail="", image_ref="x", duration_ms=1)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[LLMResult] = []
        self.last_seq: int | None = None

    async def __call__(self, result: LLMResult) -> None:
        self.calls.append(result)
        self.last_seq = len(self.calls) + 10


def write(dockerfile: str = NAIVE) -> Any:
    return reply(
        json.dumps({"dockerfile": dockerfile, "rationale": "slim python"}),
        prompt_tokens=900,
        completion_tokens=200,
    )


def fix(dockerfile: str, kind: str = "system_packages") -> Any:
    return reply(
        json.dumps(
            {
                "dockerfile": dockerfile,
                "rationale": "add headers",
                "changed_lines": ["RUN apt-get ..."],
                "fix_kind": kind,
            }
        ),
        prompt_tokens=1500,
        completion_tokens=400,
    )


def _app(tmp_path: Path, *, dockerfile: str | None = None) -> tuple[Path, AppSpec]:
    (tmp_path / "requirements.txt").write_text("flask\ngunicorn\npsycopg2==2.9.9\n")
    (tmp_path / "app.py").write_text("from flask import Flask\napp = Flask(__name__)\n")
    if dockerfile:
        (tmp_path / "Dockerfile").write_text(dockerfile)
    spec = AppSpec(
        language="python",
        framework="flask",
        start_cmd="gunicorn app:app",
        port=8000,
        health_path="/health",
        env_vars=[EnvVar(name="DATABASE_URL", required=True, secret=True)],
        has_dockerfile=dockerfile is not None,
    )
    return tmp_path, spec


async def _run(
    builder: Builder,
    root: Path,
    spec: AppSpec,
    settings: Settings,
    smokes: list[SmokeResult] | None = None,
) -> tuple[Any, list[BuildAttempt], list[str], Recorder]:
    attempts: list[BuildAttempt] = []
    notes: list[str] = []
    recorder = Recorder()

    async def on_attempt(a: BuildAttempt) -> None:
        attempts.append(a)

    async def on_note(text: str) -> None:
        notes.append(text)

    outcome = await builder.build(
        root,
        spec,
        app="legacy-flask-app",
        tag="abc123",
        run_id="run-1",
        budget=_budget(settings),
        recorder=recorder,
        on_attempt=on_attempt,
        on_note=on_note,
        check=None if smokes is None else (lambda _built: _next(smokes)),
    )
    return outcome, attempts, notes, recorder


async def _next(smokes: list[SmokeResult]) -> SmokeResult:
    return smokes.pop(0)


async def no_sleep(_s: float) -> None:
    return None


async def test_fails_twice_then_passes_with_automatic_fixes(tmp_path: Path) -> None:
    settings = _settings()
    root, spec = _app(tmp_path)
    llm = FakeLLM(
        write(), fix(FIXED), fix(FIXED + "EXPOSE 8000\n", "build_step"), settings=settings
    )
    sandbox = ScriptedSandbox(failed(PSYCOPG2), other_error(2), OK)
    outcome, attempts, notes, _ = await _run(
        Builder(llm, sandbox, sleep=no_sleep), root, spec, settings
    )

    assert outcome.ok and outcome.automatic_fix and outcome.image_id == "sha256:" + "c" * 64
    assert outcome.image_ref == "gc-build/legacy-flask-app:abc123-a3"
    assert [(a.attempt, a.ok, a.dockerfile_source) for a in attempts] == [
        (1, False, "generated"),
        (2, False, "fixed"),
        (3, True, "fixed"),
    ]
    first = attempts[0]
    assert first.dockerfile_diff == NAIVE
    assert first.failing_step == "RUN pip install --no-cache-dir -r requirements.txt"
    assert first.error_summary and "pg_config" in first.error_summary
    assert first.log_tail and len(first.log_tail) <= 8000
    assert first.model_call and first.model_call.task == "write_dockerfile"
    assert (first.model_call.prompt_tokens, first.model_call.completion_tokens) == (900, 200)
    assert attempts[1].dockerfile_diff.startswith("--- Dockerfile (attempt 1)")
    assert "+RUN apt-get install -y gcc libpq-dev" in attempts[1].dockerfile_diff
    assert attempts[1].model_call and attempts[1].model_call.task == "fix_build"
    assert attempts[1].model_call.step_seq == 12
    assert notes == [] and sandbox.pushed == []
    # The fix prompt got the failing step and the error.
    fix_prompt = llm.calls[1]["messages"][-1]["content"]
    assert "Failing step: RUN pip install --no-cache-dir -r requirements.txt" in fix_prompt
    assert "pg_config" in fix_prompt
    assert [c["model"] for c in llm.calls] == ["m/super"] * 3
    # Builder writes only inside the repo copy.
    assert sandbox.builds[0][0] == root / GENERATED_DOCKERFILE
    assert (
        (root / GENERATED_DOCKERFILE).read_text().startswith("FROM python:3.12-slim\nRUN apt-get")
    )


async def test_the_fix_for_attempt_four_escalates(tmp_path: Path) -> None:
    settings = _settings()
    root, spec = _app(tmp_path)
    llm = FakeLLM(
        write(),
        fix(FIXED, "a"),
        fix(FIXED + "#2\n", "base_image"),
        fix(FIXED + "#3\n", "build_step"),
        settings=settings,
    )
    sandbox = ScriptedSandbox(other_error(1), other_error(2), other_error(3), OK)
    outcome, attempts, _, _ = await _run(
        Builder(llm, sandbox, sleep=no_sleep), root, spec, settings
    )
    assert outcome.ok and len(attempts) == 4
    assert [c["model"] for c in llm.calls] == ["m/super", "m/super", "m/super", "m/ultra"]
    assert attempts[3].model_call and attempts[3].model_call.escalated
    assert not attempts[2].model_call or not attempts[2].model_call.escalated


async def test_escalation_stays_on_super_without_ultra(tmp_path: Path) -> None:
    settings = _settings(dev_mode_no_ultra=True)
    root, spec = _app(tmp_path)
    llm = FakeLLM(
        write(),
        fix(FIXED, "a"),
        fix(FIXED + "#2\n", "base_image"),
        fix(FIXED + "#3\n", "build_step"),
        settings=settings,
    )
    sandbox = ScriptedSandbox(other_error(1), other_error(2), other_error(3), OK)
    outcome, _, _, _ = await _run(Builder(llm, sandbox, sleep=no_sleep), root, spec, settings)
    assert outcome.ok and [c["model"] for c in llm.calls] == ["m/super"] * 4


async def test_same_error_after_the_same_kind_of_fix_is_no_progress(tmp_path: Path) -> None:
    settings = _settings()
    root, spec = _app(tmp_path)
    llm = FakeLLM(write(), fix(FIXED), fix(FIXED + "RUN true\n"), settings=settings)
    sandbox = ScriptedSandbox(failed(PSYCOPG2), failed(PSYCOPG2), failed(PSYCOPG2))
    outcome, attempts, _, _ = await _run(
        Builder(llm, sandbox, sleep=no_sleep), root, spec, settings
    )
    assert not outcome.ok and outcome.stop_reason == "no_progress"
    assert len(attempts) == 3 and len(llm.calls) == 3
    assert outcome.error and "system_packages" in outcome.error


async def test_a_fix_that_changes_nothing_is_no_progress(tmp_path: Path) -> None:
    settings = _settings()
    root, spec = _app(tmp_path)
    llm = FakeLLM(write(), fix(NAIVE), settings=settings)
    sandbox = ScriptedSandbox(failed(PSYCOPG2))
    outcome, attempts, _, _ = await _run(
        Builder(llm, sandbox, sleep=no_sleep), root, spec, settings
    )
    assert outcome.stop_reason == "no_progress" and len(attempts) == 1
    assert outcome.error == "The fix returned the same Dockerfile."


async def test_five_attempts_at_most(tmp_path: Path) -> None:
    settings = _settings()
    root, spec = _app(tmp_path)
    kinds = ["system_packages", "base_image", "build_step", "install_command"]
    llm = FakeLLM(
        write(), *(fix(FIXED + f"#{i}\n", k) for i, k in enumerate(kinds)), settings=settings
    )
    sandbox = ScriptedSandbox(*(other_error(i) for i in range(5)))
    outcome, attempts, _, _ = await _run(
        Builder(llm, sandbox, sleep=no_sleep), root, spec, settings
    )
    assert not outcome.ok and outcome.stop_reason == "max_attempts"
    assert [a.attempt for a in attempts] == [1, 2, 3, 4, 5] and len(llm.calls) == 5
    assert all(a.max_attempts == 5 for a in attempts)


async def test_network_errors_are_retried_without_an_attempt_or_a_model_call(
    tmp_path: Path,
) -> None:
    settings = _settings()
    root, spec = _app(tmp_path)
    llm = FakeLLM(write(), settings=settings)
    sandbox = ScriptedSandbox(failed(FLAKE), failed("", timed_out=True), OK)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    outcome, attempts, notes, _ = await _run(
        Builder(llm, sandbox, sleep=sleep), root, spec, settings
    )
    assert outcome.ok and not outcome.automatic_fix
    assert [a.attempt for a in attempts] == [1] and len(llm.calls) == 1
    assert slept == [10.0, 30.0]
    assert notes[0].startswith("Retrying after a network error (1 of 2, in 10 s)")
    assert notes[1].startswith("Retrying after a timeout (2 of 2, in 30 s)")


async def test_a_build_that_keeps_failing_on_the_network_counts_as_one_attempt(
    tmp_path: Path,
) -> None:
    settings = _settings()
    root, spec = _app(tmp_path)
    llm = FakeLLM(write(), fix(FIXED, "other"), settings=settings)
    sandbox = ScriptedSandbox(failed(FLAKE), failed(FLAKE), failed(FLAKE), OK)
    outcome, attempts, notes, _ = await _run(
        Builder(llm, sandbox, sleep=no_sleep), root, spec, settings
    )
    assert outcome.ok and [a.ok for a in attempts] == [False, True] and len(notes) == 2


async def test_a_repo_dockerfile_is_built_as_is_first(tmp_path: Path) -> None:
    settings = _settings()
    repo_dockerfile = "FROM python:3.12\nCOPY . .\nCMD python app.py\n"
    root, spec = _app(tmp_path, dockerfile=repo_dockerfile)
    llm = FakeLLM(settings=settings)
    sandbox = ScriptedSandbox(OK)
    outcome, attempts, _, _ = await _run(
        Builder(llm, sandbox, sleep=no_sleep), root, spec, settings
    )
    assert outcome.ok and attempts[0].dockerfile_source == "repo" and attempts[0].model_call is None
    assert llm.calls == [] and sandbox.builds[0][0] == root / "Dockerfile"
    assert (root / "Dockerfile").read_text() == repo_dockerfile


async def test_a_compose_named_dockerfile_is_the_one_built(tmp_path: Path) -> None:
    settings = _settings()
    root, spec = _app(tmp_path, dockerfile="FROM scratch\n")
    (root / "docker").mkdir()
    (root / "docker" / "prod.Dockerfile").write_text("FROM python:3.12\nCMD python app.py\n")
    spec = spec.model_copy(update={"dockerfile": "docker/prod.Dockerfile"})
    sandbox = ScriptedSandbox(OK)
    outcome, attempts, _, _ = await _run(
        Builder(FakeLLM(settings=settings), sandbox, sleep=no_sleep), root, spec, settings
    )
    assert outcome.ok and attempts[0].dockerfile_source == "repo"
    assert sandbox.builds[0][0] == root / "docker" / "prod.Dockerfile"


async def test_a_failed_model_call_stops_the_builder(tmp_path: Path) -> None:
    settings = _settings()
    root, spec = _app(tmp_path)
    llm = FakeLLM(LLMError("down"), settings=settings)
    with pytest.raises(BuilderError, match="write_dockerfile failed"):
        await _run(Builder(llm, ScriptedSandbox(), sleep=no_sleep), root, spec, settings)


async def test_smoke_uses_the_health_path(tmp_path: Path) -> None:
    settings = _settings()
    root, spec = _app(tmp_path)
    builder = Builder(FakeLLM(write(), settings=settings), ScriptedSandbox(OK), sleep=no_sleep)
    outcome, _, _, _ = await _run(builder, root, spec, settings)
    smoke = await builder.smoke(outcome, spec, [], run_id="run-1")
    assert smoke.ok and smoke.checks[0].path == "/health"


def test_transient_detection_on_real_logs() -> None:
    assert is_transient(failed(FLAKE))
    assert not is_transient(failed(PSYCOPG2))
    assert is_transient(failed("", timed_out=True))
    assert not is_transient(OK)


def test_the_same_error_has_the_same_signature_across_runs() -> None:
    a = failed(PSYCOPG2)
    b = failed(PSYCOPG2.replace("12.25", "13.01").replace("#8 ", "#9 "))
    assert signature(a) == signature(b)
    assert signature(a) != signature(other_error(1))


def test_model_dockerfiles_are_cleaned() -> None:
    from app.agents.builder.builder import clean_dockerfile

    assert clean_dockerfile("```dockerfile\nFROM python:3.12-slim\nCMD x\n```") == (
        "FROM python:3.12-slim\nCMD x\n"
    )
    assert clean_dockerfile('FROM python:3.12-slim\\nCMD ["gunicorn"]') == (
        'FROM python:3.12-slim\nCMD ["gunicorn"]\n'
    )
    assert clean_dockerfile("FROM a\nRUN echo '\\n'\n") == "FROM a\nRUN echo '\\n'\n"


def test_exec_form_commands_with_variables_run_through_a_shell() -> None:
    """Docker does not expand ${API_PORT:-8080} in exec form; uvicorn got it as the port."""
    from app.agents.builder.builder import clean_dockerfile

    model = 'FROM a\nCMD ["uvicorn", "app.main:app", "--port", "${API_PORT:-8080}"]'
    assert clean_dockerfile(model) == (
        'FROM a\nCMD exec uvicorn app.main:app --port "${API_PORT:-8080}"\n'
    )
    kept = 'CMD ["node", "server.js"]\nENTRYPOINT ["sh", "-c", "exec x --port $PORT"]'
    assert clean_dockerfile(kept) == kept + "\n"


async def test_a_build_service_outage_stops_at_once_without_a_fix(tmp_path: Path) -> None:
    """No Dockerfile fix can help when the build daemon refuses the connection."""
    settings = _settings()
    root, spec = _app(tmp_path)
    llm = FakeLLM(write(), settings=settings)
    down = unavailable_text(
        "permission denied while trying to connect to the docker API at unix:///run/x.sock"
    )
    sandbox = ScriptedSandbox(
        BuildResult(False, down, None, 5, error_lines=[down], unavailable=True)
    )
    outcome, attempts, _, _ = await _run(
        Builder(llm, sandbox, sleep=no_sleep), root, spec, settings
    )
    assert not outcome.ok and outcome.stop_reason == "unavailable" and len(attempts) == 1
    assert len(llm.calls) == 1  # the first Dockerfile only, no fix_build
    assert outcome.error == down


def test_daemon_connection_errors_are_recognized() -> None:
    assert daemon_unreachable(
        "permission denied while trying to connect to the docker API at unix:///run/x.sock"
    )
    assert daemon_unreachable("Cannot connect to the Docker daemon at unix:///var/run/docker.sock.")
    assert not daemon_unreachable("ERROR: failed to solve: process did not complete: exit code 1")


CRASHED = SmokeResult(
    ok=False,
    checks=[],
    duration_ms=600,
    logs_tail="exec: uvicorn: not found",
    detail="the app exited at start (exit code 127)",
)
ANSWERED = SmokeResult(
    ok=True, checks=[SmokeCheck("/health", True, 200, "HTTP 200")], duration_ms=40
)


async def test_a_failed_smoke_test_goes_back_to_builder(tmp_path: Path) -> None:
    """The image built but the app crashed: Builder reads its output and fixes the Dockerfile."""
    settings = _settings()
    root, spec = _app(tmp_path)
    llm = FakeLLM(write(), fix(FIXED, "other"), settings=settings)
    builder = Builder(llm, ScriptedSandbox(OK, OK), sleep=no_sleep)
    outcome, attempts, _, _ = await _run(builder, root, spec, settings, [CRASHED, ANSWERED])
    assert outcome.ok and outcome.smoke is ANSWERED and outcome.automatic_fix
    first, second = attempts
    assert not first.ok and first.failing_step == SMOKE_STEP
    assert first.error_summary == "exec: uvicorn: not found"  # the app's line names the cause
    assert second.ok
    asked = llm.requests[-1][1]["content"]
    assert "exec: uvicorn: not found" in asked and "Failing step: smoke test" in asked


async def test_the_last_attempt_keeps_its_failed_smoke_test(tmp_path: Path) -> None:
    settings = _settings()
    root, spec = _app(tmp_path)
    builder = Builder(
        FakeLLM(write(), settings=settings), ScriptedSandbox(OK), sleep=no_sleep, max_attempts=1
    )
    outcome, attempts, _, _ = await _run(builder, root, spec, settings, [CRASHED])
    assert outcome.ok and outcome.smoke is CRASHED and attempts[0].ok


UNHEALTHY = SmokeResult(
    ok=False,
    checks=[SmokeCheck("/health", False, 503, "HTTP 503")],
    duration_ms=60_000,
    logs_tail='{"msg": "health check failed: database unreachable"}',
    detail="/health did not answer 2xx/3xx within 60s (HTTP 503)",
)


async def test_an_app_that_answers_is_not_sent_back_to_builder(tmp_path: Path) -> None:
    """A 503 from the health check is the sandbox having no database, not the Dockerfile."""
    settings = _settings()
    root, spec = _app(tmp_path)
    llm = FakeLLM(write(), settings=settings)
    builder = Builder(llm, ScriptedSandbox(OK), sleep=no_sleep)
    outcome, attempts, _, _ = await _run(builder, root, spec, settings, [UNHEALTHY])
    assert outcome.ok and outcome.smoke is UNHEALTHY and len(attempts) == 1 and attempts[0].ok
    assert len(llm.requests) == 1  # no fix asked for


def test_runtime_env_never_goes_into_the_image() -> None:
    from app.agents.builder.builder import drop_runtime_env

    dockerfile = (
        "FROM python:3.12-slim\n"
        "ENV PYTHONUNBUFFERED=1 DATABASE_URL=postgresql://user:pass@localhost/db\n"
        "ENV STRIPE_API_KEY placeholder\n"
        "ENV FEATURE_FLAGS=on LOG_LEVEL=info\n"
        'CMD ["gunicorn", "app:app"]\n'
    )
    text, removed = drop_runtime_env(dockerfile, {"FEATURE_FLAGS"})
    assert removed == ["DATABASE_URL", "STRIPE_API_KEY", "FEATURE_FLAGS"]
    assert text == (
        "FROM python:3.12-slim\n"
        "ENV PYTHONUNBUFFERED=1\n"
        "ENV LOG_LEVEL=info\n"
        'CMD ["gunicorn", "app:app"]\n'
    )


async def test_builder_removes_placeholder_env_and_says_so(tmp_path: Path) -> None:
    settings = _settings()
    root, spec = _app(tmp_path)
    llm = FakeLLM(write(NAIVE + "ENV DATABASE_URL=sqlite:///tmp.db\n"), settings=settings)
    builder = Builder(llm, ScriptedSandbox(OK), sleep=no_sleep)
    outcome, _, notes, _ = await _run(builder, root, spec, settings)
    assert "DATABASE_URL" not in outcome.dockerfile
    assert notes and notes[0].startswith("Removed ENV DATABASE_URL from the Dockerfile")
