"""DockerSandbox: builds, smoke tests, and pushes through the Docker CLI (dind over TLS locally).

Smoke containers run with no capabilities, no-new-privileges, 1 CPU, 512 MB, 256 processes, and
a tmpfs /tmp, never privileged. Env values reach `docker run` through the CLI's environment
(`-e NAME` without a value), never argv. Every container is removed, whatever happens.
"""

import asyncio
import re
import secrets
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from app.config import Settings
from app.sandbox.base import (
    LABEL,
    LOG_TAIL_CHARS,
    RUN_LABEL,
    BuildResult,
    PushResult,
    PushTarget,
    SandboxError,
    SmokeCheck,
    SmokeEnv,
    SmokeResult,
)
from app.sandbox.logs import Scrubber, error_lines, failing_step, tail
from app.sandbox.runner import CommandRunner, run_docker

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_DIGEST = re.compile(r"digest: (sha256:[0-9a-f]{64})")
# Fargate and the kind nodes run x86_64.
PLATFORM = "linux/amd64"
SMOKE_LIMITS = (
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
    "--cpus",
    "1",
    "--memory",
    "512m",
    "--pids-limit",
    "256",
    "--tmpfs",
    "/tmp:rw,nosuid,size=64m",  # noqa: S108 (inside the smoke container, not ours)
)
_POLL_SECONDS = 1.0
_HTTP_TIMEOUT = 3.0


def _check_run_id(run_id: str) -> None:
    if not _RUN_ID.match(run_id):
        raise ValueError(f"bad run id for labels: {run_id!r}")


def _ms(start: float) -> int:
    return round((time.monotonic() - start) * 1000)


# Fetches one path of the smoke container's app and says how it answered.
Getter = Callable[[str], Awaitable["SmokeCheck"]]


class DockerSandbox:
    def __init__(self, settings: Settings, runner: CommandRunner = run_docker) -> None:
        self.settings = settings
        self._docker = runner

    # --- build ------------------------------------------------------------------------------

    async def build(
        self, context_dir: Path, dockerfile: Path, tag: str, *, run_id: str
    ) -> BuildResult:
        _check_run_id(run_id)
        start = time.monotonic()
        result = await self._docker(
            [
                "build",
                "--progress=plain",
                "--platform",
                PLATFORM,
                "--file",
                str(dockerfile),
                "--tag",
                tag,
                "--label",
                f"{LABEL}=1",
                "--label",
                f"{RUN_LABEL}={run_id}",
                str(context_dir),
            ],
            timeout=self.settings.build_timeout_seconds,
        )
        log = result.output
        if result.timed_out:
            limit = self.settings.build_timeout_seconds
            log += f"\n[groundcrew] build timed out after {limit:.0f}s"
        ok = result.ok
        return BuildResult(
            ok=ok,
            log_tail=tail(log, LOG_TAIL_CHARS),
            image_ref=tag if ok else None,
            duration_ms=_ms(start),
            failing_step=None if ok else failing_step(Scrubber()(log)),
            error_lines=[] if ok else error_lines(Scrubber()(log)),
            timed_out=result.timed_out,
        )

    # --- smoke ------------------------------------------------------------------------------

    async def run_smoke(
        self,
        image_ref: str,
        port: int,
        env: list[SmokeEnv],
        *,
        run_id: str,
        health_path: str | None = None,
    ) -> SmokeResult:
        _check_run_id(run_id)
        start = time.monotonic()
        for var in env:
            if not _ENV_NAME.match(var.name):
                return SmokeResult(False, [], _ms(start), detail=f"bad env var name {var.name!r}")
        scrub = Scrubber([v.value.get_secret_value() for v in env if v.secret])
        name = f"gc-smoke-{run_id[:24]}-{secrets.token_hex(3)}"
        args = [
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            f"{LABEL}=1",
            "--label",
            f"{RUN_LABEL}={run_id}",
            *SMOKE_LIMITS,
            *self._smoke_run_args(port),
        ]
        for var in env:
            args += ["--env", var.name]
        args.append(image_ref)
        try:
            started = await self._docker(
                args,
                timeout=60,
                env={v.name: v.value.get_secret_value() for v in env},
            )
            if not started.ok:
                return SmokeResult(
                    False,
                    [],
                    _ms(start),
                    detail=f"container did not start: {scrub(started.stderr)[-500:]}",
                )
            async with self._reach(name, port, run_id) as reached:
                if isinstance(reached, str):
                    return SmokeResult(False, [], _ms(start), detail=reached)
                checks, detail = await self._probe(name, reached, health_path)
            ok = bool(checks) and checks[0].ok
            logs = None if ok else await self._logs(name, scrub)
            return SmokeResult(ok, checks, _ms(start), logs_tail=logs, detail=detail)
        finally:
            await self._docker(["rm", "--force", "--volumes", name], timeout=60)

    def _smoke_run_args(self, port: int) -> list[str]:
        """Published on the daemon's host, where the worker reaches it (SANDBOX_SMOKE_HOST)."""
        return ["--publish", str(port)]

    @asynccontextmanager
    async def _reach(self, name: str, port: int, run_id: str) -> AsyncIterator[Getter | str]:
        """How the probe reaches the app: a getter, or why it cannot (a message)."""
        host_port = await self._host_port(name, port)
        if host_port is None:
            yield f"port {port} was not published"
            return
        base = f"http://{self.settings.sandbox_smoke_host}:{host_port}"
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=False) as client:

            async def get(path: str) -> SmokeCheck:
                return await _get(client, base, path)

            yield get

    async def _host_port(self, name: str, port: int) -> int | None:
        result = await self._docker(["port", name, f"{port}/tcp"], timeout=30)
        for line in result.stdout.splitlines():
            _, _, value = line.rpartition(":")
            if value.isdigit():
                return int(value)
        return None

    async def _running(self, name: str) -> tuple[bool, str]:
        result = await self._docker(
            ["inspect", "--format", "{{.State.Running}} {{.State.ExitCode}}", name], timeout=30
        )
        running, _, code = result.stdout.strip().partition(" ")
        return running == "true", code

    async def _probe(
        self, name: str, get: Getter, health_path: str | None
    ) -> tuple[list[SmokeCheck], str]:
        """Poll the health path (or / without one) until it answers 2xx/3xx, the container
        exits, or the smoke timeout passes. With a health path, / is then probed once and
        recorded; it does not decide the result (APIs often have no / route)."""
        first = health_path or "/"
        deadline = time.monotonic() + self.settings.sandbox_smoke_timeout_seconds
        last = "no answer yet"
        while True:
            check = await get(first)
            if check.ok:
                break
            last = check.detail
            running, code = await self._running(name)
            if not running:
                return [check], f"container exited with code {code} before {first} answered"
            if time.monotonic() >= deadline:
                return [check], (
                    f"{first} did not answer 2xx/3xx within "
                    f"{self.settings.sandbox_smoke_timeout_seconds:.0f}s ({last})"
                )
            await asyncio.sleep(_POLL_SECONDS)
        checks = [check]
        if first != "/":
            checks.append(await get("/"))
        return checks, f"{first} answered {check.status_code}"

    async def _logs(self, name: str, scrub: Scrubber) -> str:
        result = await self._docker(["logs", "--tail", "200", name], timeout=30)
        return tail(result.output, LOG_TAIL_CHARS, scrub)

    # --- push -------------------------------------------------------------------------------

    async def push(self, image_ref: str, target: PushTarget) -> PushResult:
        """Tag and push. A registry login goes into a temporary DOCKER_CONFIG through
        --password-stdin, and the password is scrubbed from any error."""
        start = time.monotonic()
        scrub = Scrubber()
        timeout = self.settings.sandbox_push_timeout_seconds
        with tempfile.TemporaryDirectory(prefix="groundcrew-docker-") as config:
            env = {"DOCKER_CONFIG": config}
            if target.login is not None:
                password = target.login.password.get_secret_value()
                scrub.add(password)
                login = await self._docker(
                    [
                        "login",
                        "--username",
                        target.login.username,
                        "--password-stdin",
                        target.login.registry,
                    ],
                    timeout=60,
                    stdin=password,
                    env=env,
                )
                if not login.ok:
                    raise SandboxError(f"registry login failed: {scrub(login.output)[-300:]}")
            tagged = await self._docker(["tag", image_ref, target.image], timeout=60, env=env)
            if not tagged.ok:
                raise SandboxError(f"docker tag failed: {scrub(tagged.output)[-300:]}")
            pushed = await self._docker(["push", target.image], timeout=timeout, env=env)
            if not pushed.ok:
                why = "timed out" if pushed.timed_out else scrub(pushed.output)[-500:]
                raise SandboxError(f"docker push {target.image} failed: {why}")
        digest = _DIGEST.search(pushed.output)
        return PushResult(target.image, digest.group(1) if digest else None, _ms(start))

    async def image_id(self, image_ref: str) -> str:
        """The image's content ID (sha256:...), which the approved plan pins."""
        found = await self._docker(
            ["image", "inspect", "--format", "{{.Id}}", image_ref], timeout=30
        )
        if found.returncode != 0 or not found.stdout.strip().startswith("sha256:"):
            raise SandboxError(f"Could not read the ID of {image_ref}.")
        return found.stdout.strip()

    # --- cleanup ----------------------------------------------------------------------------

    async def cleanup(self, run_id: str) -> None:
        """Remove this run's containers and images (the build cache stays)."""
        _check_run_id(run_id)
        selector = f"label={RUN_LABEL}={run_id}"
        containers = await self._docker(
            ["ps", "--all", "--quiet", "--filter", selector], timeout=30
        )
        ids = containers.stdout.split()
        if ids:
            await self._docker(["rm", "--force", "--volumes", *ids], timeout=60)
        images = await self._docker(["images", "--quiet", "--filter", selector], timeout=30)
        image_ids = sorted(set(images.stdout.split()))
        if image_ids:
            await self._docker(["rmi", "--force", *image_ids], timeout=120)


async def _get(client: httpx.AsyncClient, base: str, path: str) -> SmokeCheck:
    try:
        resp = await client.get(base + path)
    except httpx.HTTPError as exc:
        return SmokeCheck(path, False, None, type(exc).__name__)
    ok = 200 <= resp.status_code < 400
    return SmokeCheck(path, ok, resp.status_code, f"HTTP {resp.status_code}")
