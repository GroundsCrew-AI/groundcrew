"""IsolatedSandbox: builds and smoke tests of untrusted code on the platform host's own build
daemon (SANDBOX_PROVIDER=isolated; docs/phase6-plan.md section 6, infra/platform/host).

That daemon is rootless (user gcbuild) and runs every container in gVisor, so neither a build
step nor the app being smoke-tested ever runs on the host kernel. The host firewall gives the
gcbuild user no route to the metadata service, the app's own containers, or private networks;
its systemd slice caps memory, tasks, and CPU, and one build runs at a time.

Per run:
- **build**: a BuildKit container of its own (`gcb-bk-<run>`, BuildKit's rootless image,
  unprivileged, no host mounts), driven by `docker buildx` through the remote driver, the image
  loaded back into the daemon. Builder retries of the same run reuse its cache. A build that
  times out takes the BuildKit container with it, so it really stops.
- **smoke**: the app container publishes no port. A probe container on the same network asks it
  over HTTP from inside the daemon (Docker's embedded DNS does not work under gVisor, so the
  probe uses the app container's address).
- **cleanup**: every container, volume, image, and the builder of the run.

Docker's per-container --memory and --pids-limit are passed as before but not enforced under
gVisor in a rootless daemon; the gcbuild slice enforces the ceiling (one build at a time).
"""

import os
import re
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from app.config import Settings
from app.sandbox.base import LABEL, LOG_TAIL_CHARS, RUN_LABEL, BuildResult, SmokeCheck
from app.sandbox.docker import PLATFORM, DockerSandbox, Getter, _check_run_id, _ms
from app.sandbox.logs import Scrubber, error_lines, failing_step, tail
from app.sandbox.runner import CommandRunner, run_docker

_IP = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_CODE = re.compile(r"(\d{3})\s*$")


def builder_name(run_id: str) -> str:
    return f"gcb-{run_id[:40]}"


def buildkit_container(run_id: str) -> str:
    return f"gcb-bk-{run_id[:40]}"


def probe_container(run_id: str) -> str:
    return f"gcb-probe-{run_id[:40]}"


class IsolatedSandbox(DockerSandbox):
    def __init__(self, settings: Settings, runner: CommandRunner = run_docker) -> None:
        super().__init__(settings, runner)
        # buildx keeps its builder list here, not in a home directory (the worker's root
        # filesystem is read-only).
        self._buildx_config = os.environ.get("BUILDX_CONFIG") or tempfile.mkdtemp(
            prefix="groundcrew-buildx-"
        )

    def _buildx_env(self) -> dict[str, str]:
        return {"BUILDX_CONFIG": self._buildx_config}

    # --- build ------------------------------------------------------------------------------

    async def _ensure_builder(self, run_id: str) -> str | None:
        """Start this run's BuildKit container and register it with buildx. None, or why not."""
        container = buildkit_container(run_id)
        running = await self._docker(
            ["inspect", "--format", "{{.State.Running}}", container], timeout=30
        )
        if running.stdout.strip() != "true":
            await self._docker(["rm", "--force", "--volumes", container], timeout=60)
            started = await self._docker(
                [
                    "run",
                    "--detach",
                    "--name",
                    container,
                    "--label",
                    f"{LABEL}=1",
                    "--label",
                    f"{RUN_LABEL}={run_id}",
                    "--env",
                    "BUILDKITD_FLAGS=--oci-worker-no-process-sandbox",
                    self.settings.sandbox_buildkit_image,
                ],
                timeout=120,
            )
            if not started.ok:
                return f"the build sandbox did not start: {started.stderr.strip()[-300:]}"
        name = builder_name(run_id)
        known = await self._docker(["buildx", "inspect", name], timeout=30, env=self._buildx_env())
        if not known.ok:
            created = await self._docker(
                [
                    "buildx",
                    "create",
                    "--name",
                    name,
                    "--driver",
                    "remote",
                    f"docker-container://{container}",
                ],
                timeout=60,
                env=self._buildx_env(),
            )
            if not created.ok:
                return f"the build sandbox was not reachable: {created.stderr.strip()[-300:]}"
        return None

    async def build(
        self, context_dir: Path, dockerfile: Path, tag: str, *, run_id: str
    ) -> BuildResult:
        _check_run_id(run_id)
        start = time.monotonic()
        problem = await self._ensure_builder(run_id)
        if problem is not None:
            return BuildResult(
                ok=False,
                log_tail=problem,
                image_ref=None,
                duration_ms=_ms(start),
                error_lines=[problem],
            )
        limit = self.settings.build_timeout_seconds
        result = await self._docker(
            [
                "buildx",
                "build",
                "--builder",
                builder_name(run_id),
                "--progress=plain",
                "--platform",
                PLATFORM,
                "--load",
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
            timeout=limit,
            env=self._buildx_env(),
        )
        log = result.output
        if result.timed_out:
            # The CLI is gone, but BuildKit would keep going: stop its container.
            await self._docker(
                ["rm", "--force", "--volumes", buildkit_container(run_id)], timeout=60
            )
            await self._docker(
                ["buildx", "rm", builder_name(run_id)], timeout=30, env=self._buildx_env()
            )
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

    def _smoke_run_args(self, port: int) -> list[str]:
        return []  # no published port: the probe asks from inside the daemon

    @asynccontextmanager
    async def _reach(self, name: str, port: int, run_id: str) -> AsyncIterator[Getter | str]:
        found = await self._docker(
            [
                "inspect",
                "--format",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
                name,
            ],
            timeout=30,
        )
        ip = next((a for a in found.stdout.split() if _IP.match(a)), None)
        if ip is None:
            yield "the app container has no address in the build sandbox"
            return
        probe = probe_container(run_id)
        await self._docker(["rm", "--force", "--volumes", probe], timeout=60)
        started = await self._docker(
            [
                "run",
                "--detach",
                "--name",
                probe,
                "--label",
                f"{LABEL}=1",
                "--label",
                f"{RUN_LABEL}={run_id}",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                self.settings.sandbox_probe_image,
                "sleep",
                "600",
            ],
            timeout=120,
        )
        if not started.ok:
            yield f"the smoke probe did not start: {started.stderr.strip()[-300:]}"
            return
        base = f"http://{ip}:{port}"

        async def get(path: str) -> SmokeCheck:
            answered = await self._docker(
                [
                    "exec",
                    probe,
                    "curl",
                    "--silent",
                    "--output",
                    "/dev/null",
                    "--max-time",
                    "3",
                    "--write-out",
                    "%{http_code}",
                    base + path,
                ],
                timeout=15,
            )
            code = _CODE.search(answered.stdout.strip())
            status = int(code.group(1)) if code else 0
            if status == 0:
                return SmokeCheck(path, False, None, "no answer")
            return SmokeCheck(path, 200 <= status < 400, status, f"HTTP {status}")

        try:
            yield get
        finally:
            await self._docker(["rm", "--force", "--volumes", probe], timeout=60)

    # --- cleanup ----------------------------------------------------------------------------

    async def cleanup(self, run_id: str) -> None:
        """Remove the run's builder (its cache with it), containers, volumes, and images."""
        _check_run_id(run_id)
        await self._docker(
            ["buildx", "rm", builder_name(run_id)], timeout=30, env=self._buildx_env()
        )
        await super().cleanup(run_id)
