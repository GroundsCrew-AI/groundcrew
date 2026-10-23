"""Build sandbox: build an image, smoke-test it, push it (docs/phase4-infra.md, Sandbox).

sandbox = get_sandbox(settings)
built = await sandbox.build(repo_dir, repo_dir / "Dockerfile", "gc-build/app:abc", run_id=...)
"""

from app.config import Settings
from app.sandbox.base import (
    BuildResult,
    PushResult,
    PushTarget,
    RegistryLogin,
    Sandbox,
    SandboxError,
    SmokeCheck,
    SmokeEnv,
    SmokeResult,
)
from app.sandbox.docker import DockerSandbox
from app.sandbox.isolated import IsolatedSandbox
from app.sandbox.nebius import NebiusSandbox

__all__ = [
    "BuildResult",
    "DockerSandbox",
    "IsolatedSandbox",
    "NebiusSandbox",
    "PushResult",
    "PushTarget",
    "RegistryLogin",
    "Sandbox",
    "SandboxError",
    "SmokeCheck",
    "SmokeEnv",
    "SmokeResult",
    "get_sandbox",
]


def get_sandbox(settings: Settings) -> Sandbox:
    if settings.sandbox_provider == "nebius":
        return NebiusSandbox(settings)
    if settings.sandbox_provider == "isolated":
        return IsolatedSandbox(settings)
    return DockerSandbox(settings)
