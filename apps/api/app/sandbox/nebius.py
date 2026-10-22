"""Nebius sandbox: not built yet. TODO(nebius-sandbox): run builds and smoke tests in Nebius
(Serverless Jobs or a build VM) so a hosted Groundcrew never runs a privileged Docker daemon
next to the API (docs/phase4-infra.md, Build daemon). Same Sandbox protocol as DockerSandbox."""

from pathlib import Path

from app.config import Settings
from app.sandbox.base import (
    BuildResult,
    PushResult,
    PushTarget,
    SandboxError,
    SmokeEnv,
    SmokeResult,
)

_TODO = "SANDBOX_PROVIDER=nebius is not implemented yet (TODO(nebius-sandbox)); use docker"


class NebiusSandbox:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def build(
        self, context_dir: Path, dockerfile: Path, tag: str, *, run_id: str
    ) -> BuildResult:
        raise SandboxError(_TODO)

    async def run_smoke(
        self,
        image_ref: str,
        port: int,
        env: list[SmokeEnv],
        *,
        run_id: str,
        health_path: str | None = None,
    ) -> SmokeResult:
        raise SandboxError(_TODO)

    async def push(self, image_ref: str, target: PushTarget) -> PushResult:
        raise SandboxError(_TODO)

    async def push_to_groundcrew(
        self, image_ref: str, registry_url: str, repository: str, tag: str
    ) -> PushResult:
        raise SandboxError(_TODO)

    async def image_id(self, image_ref: str) -> str:
        raise SandboxError(_TODO)

    async def cleanup(self, run_id: str) -> None:
        raise SandboxError(_TODO)
