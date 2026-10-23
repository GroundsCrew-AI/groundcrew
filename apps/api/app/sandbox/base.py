"""The sandbox contract: build an image, smoke-test it, push it. Builder uses only this, so the
build backend (dind locally, a separate build host or Nebius later) changes configuration,
not code (docs/phase4-infra.md, Sandbox)."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import SecretStr

# Every container and image the sandbox creates carries these labels, so cleanup finds them.
LABEL = "groundcrew"
RUN_LABEL = "groundcrew.run"
LOG_TAIL_CHARS = 8000


class SandboxError(RuntimeError):
    """The sandbox itself failed (daemon unreachable, push refused), not the app's build."""


@dataclass(frozen=True)
class BuildResult:
    ok: bool
    # Last LOG_TAIL_CHARS of the build output, redacted.
    log_tail: str
    # The tag that was built, when ok.
    image_ref: str | None
    duration_ms: int
    # The Dockerfile instruction that failed (BuildKit's `process "..." did not complete`), and
    # the log lines that look like the cause. Both empty when ok or unknown.
    failing_step: str | None = None
    error_lines: list[str] = field(default_factory=list)
    timed_out: bool = False
    # The build service itself failed (its daemon did not answer or refused the connection),
    # not the Dockerfile: no Dockerfile fix can help, so Builder stops at once.
    unavailable: bool = False


# What the Docker CLI prints when it cannot use the daemon at all.
_DAEMON_UNREACHABLE = re.compile(
    r"Cannot connect to the Docker daemon|permission denied while trying to connect to the "
    r"docker (?:API|daemon)|error during connect|Is the docker daemon running",
    re.IGNORECASE,
)


def daemon_unreachable(output: str) -> bool:
    return bool(_DAEMON_UNREACHABLE.search(output))


def unavailable_text(detail: str) -> str:
    return (
        "Groundcrew's build service is not available right now, so nothing was built. This is "
        f"on Groundcrew's side, not in your repository; try again later. ({detail.strip()[-300:]})"
    )


@dataclass(frozen=True)
class SmokeCheck:
    path: str
    ok: bool
    status_code: int | None
    detail: str


@dataclass(frozen=True)
class SmokeResult:
    ok: bool
    checks: list[SmokeCheck]
    duration_ms: int
    # The container's own output on failure (redacted, secret env values scrubbed).
    logs_tail: str | None = None
    detail: str = ""

    @property
    def answered(self) -> bool:
        """The app answered HTTP (any status): the image runs and listens on its port."""
        return any(c.status_code for c in self.checks)


@dataclass(frozen=True)
class RegistryLogin:
    registry: str
    username: str
    password: SecretStr


@dataclass(frozen=True)
class PushTarget:
    """Where an image goes: `image` is the full destination name with tag. `login` is None for
    the local kind registry (plain HTTP, no auth)."""

    image: str
    login: RegistryLogin | None = None


@dataclass(frozen=True)
class PushResult:
    image: str
    # Registry digest (sha256:...) when the push reported one.
    digest: str | None
    duration_ms: int


@dataclass(frozen=True)
class SmokeEnv:
    name: str
    value: SecretStr
    secret: bool = False


class Sandbox(Protocol):
    async def build(
        self, context_dir: Path, dockerfile: Path, tag: str, *, run_id: str
    ) -> BuildResult: ...

    async def run_smoke(
        self,
        image_ref: str,
        port: int,
        env: list[SmokeEnv],
        *,
        run_id: str,
        health_path: str | None = None,
    ) -> SmokeResult: ...

    async def push(self, image_ref: str, target: PushTarget) -> PushResult: ...

    async def push_to_groundcrew(
        self, image_ref: str, registry_url: str, repository: str, tag: str
    ) -> PushResult:
        """Hand the image to the Groundcrew registry (app/registry.py) from outside the build
        service, which cannot reach it."""
        ...

    async def image_id(self, image_ref: str) -> str: ...

    async def cleanup(self, run_id: str) -> None: ...
