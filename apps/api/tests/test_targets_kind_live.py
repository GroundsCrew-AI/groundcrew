"""The Kubernetes target on the live kind cluster (RUN_LIVE_TESTS=1, after make kind-up and
make kind-kubeconfig, run on this machine with the host Docker daemon).

One pass through everything M3 promises, with real approvals and tokens:
build and smoke-test in the sandbox -> plan (push listed first) -> preflight -> approve ->
apply (push, then namespace, RoleBinding, Secret, Deployment, Service, Ingress) -> verify
through ingress-nginx -> logs -> a second deploy -> rollback to revision 1 (emergency
approval) -> delete as a first-deploy rollback (emergency approval).
"""

import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.connectors.credentials import CredentialsProvider
from app.db.models import Run, Workspace
from app.db.session import SessionFactory
from app.policy.actions import TARGET_APPLY, TARGET_ROLLBACK
from app.sandbox import DockerSandbox
from app.sandbox.base import SmokeEnv
from app.targets import K8sTarget
from app.targets.base import Deployment, DeploySpec, EnvValue

from .target_fakes import approved_action

ROOT = Path(__file__).resolve().parents[3]
HOST_KUBECONFIG = ROOT / ".kind" / "groundcrew-deployer.host.kubeconfig"
CONNECTION = uuid.UUID("00000000-0000-0000-0000-000000000001")
APP = "m3-live"
SECRET = "live-secret-value-not-printed"  # noqa: S105

pytestmark = [pytest.mark.live, pytest.mark.docker, pytest.mark.anyio]

# Logs MODE and only a hash of DATABASE_URL; answers 200 with MODE on every path.
APP_PY = """import hashlib, http.server, os
mode = os.environ.get("MODE", "?")
digest = hashlib.sha256(os.environ.get("DATABASE_URL", "").encode()).hexdigest()[:12]
print("MODE=" + mode, "DATABASE_URL sha256:" + digest, flush=True)
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(mode.encode())
http.server.ThreadingHTTPServer(("", 8000), H).serve_forever()
"""
DOCKERFILE = (
    'FROM python:3.12-slim\nCOPY app.py /app.py\nEXPOSE 8000\nCMD ["python", "-u", "/app.py"]\n'
)


class HostKubeconfig:
    def kubeconfig(self, connection_id: uuid.UUID) -> Path:
        return HOST_KUBECONFIG

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"not used: {name}")


def _image_id(ref: str) -> str:
    out = subprocess.run(  # noqa: S603
        ["docker", "image", "inspect", "--format", "{{.Id}}", ref],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


async def _build(
    sandbox: DockerSandbox, tmp: Path, run_id: uuid.UUID, mode: str
) -> tuple[str, str, str]:
    (tmp / "app.py").write_text(APP_PY)
    (tmp / "Dockerfile").write_text(DOCKERFILE)
    tag = uuid.uuid4().hex[:12]
    ref = f"gc-live/{APP}:{tag}"
    built = await sandbox.build(tmp, tmp / "Dockerfile", ref, run_id=str(run_id))
    assert built.ok, built.log_tail
    smoke = await sandbox.run_smoke(
        ref,
        8000,
        [SmokeEnv(name="MODE", value=SecretStr(mode))],
        run_id=str(run_id),
        health_path="/health",
    )
    assert smoke.ok, smoke.detail
    return ref, _image_id(ref), tag


async def test_kind_target_end_to_end(
    session_factory: SessionFactory, workspace: Workspace, tmp_path: Path
) -> None:
    if not HOST_KUBECONFIG.exists():
        pytest.fail("No deployer kubeconfig: run make kind-up and make kind-kubeconfig first.")
    settings = Settings(
        gc_kind_registry="localhost:5001",
        gc_verify_timeout_seconds=180,
        sandbox_smoke_host="localhost",
    )
    sandbox = DockerSandbox(settings)
    target = K8sTarget(
        settings,
        cast(CredentialsProvider, HostKubeconfig()),
        CONNECTION,
        cluster="kind-groundcrew",
        session_factory=session_factory,
    )
    report: list[str] = []
    first: Deployment | None = None
    run_ids: list[str] = []
    try:

        async def deploy(mode: str) -> tuple[Run, Deployment]:
            run, _, _ = await approved_action(
                session_factory, workspace, action=TARGET_APPLY, plan_hash="0" * 64
            )
            run_ids.append(str(run.id))
            ref, image_id, tag = await _build(sandbox, tmp_path, run.id, mode)
            spec = DeploySpec(
                run_id=run.id,
                connection_id=CONNECTION,
                app=APP,
                source_image=ref,
                source_image_id=image_id,
                smoke_passed=True,
                tag=tag,
                port=8000,
                health_path="/health",
                env=[
                    EnvValue(name="DATABASE_URL", value=SECRET, secret=True),
                    EnvValue(name="MODE", value=mode),
                ],
                required_env=["DATABASE_URL"],
            )
            plan = await target.plan(spec)
            assert plan.steps[0].action == "push"
            assert SECRET not in plan.model_dump_json()
            checks = await target.preflight(plan)
            assert all(c.status == "pass" for c in checks), checks
            report.append(f"plan {mode}: " + ", ".join(f"{s.action} {s.kind}" for s in plan.steps))
            _, _, token = await approved_action(
                session_factory, workspace, action=TARGET_APPLY, plan_hash=plan.plan_hash(), run=run
            )
            assert token is not None
            deployment = await target.apply(plan, spec, token, sandbox)
            health = await target.verify(deployment)
            assert health.ok, (health.checks, health.logs_tail)
            report.append(
                f"live {mode}: revision {deployment.revision}, "
                + ", ".join(f"{c.name} {c.detail}" for c in health.checks)
                + f", {health.duration_ms} ms"
            )
            return run, deployment

        run, first = await deploy("blue")
        lines = await target.logs(first, datetime.now(UTC) - timedelta(minutes=5))
        assert any("MODE=blue" in line.message for line in lines), lines
        assert not any(SECRET in line.message for line in lines)
        run, second = await deploy("green")
        assert second.previous_revision == first.revision

        back = await target.rollback_plan(second)
        _, _, token = await approved_action(
            session_factory, workspace, action=TARGET_ROLLBACK, plan_hash=back.plan_hash(), run=run
        )
        rolled = await target.rollback(back, token)  # type: ignore[arg-type]
        health = await target.verify(rolled)
        assert health.ok and rolled.image == first.image, (rolled, health.checks)
        report.append(
            f"rollback to {back.rollback_to_revision}: "
            f"now revision {rolled.revision}, {rolled.image}"
        )
    finally:
        if first is not None:
            # Clean up the way a first-deploy rollback would: delete the namespace, with its
            # own emergency approval on the first run.
            delete = await target.rollback_plan(
                first.model_copy(update={"previous_revision": None})
            )
            _, _, token = await approved_action(
                session_factory,
                workspace,
                action=TARGET_ROLLBACK,
                plan_hash=delete.plan_hash(),
                run=await _run(session_factory, first.run_id),
            )
            await target.rollback(delete, token)  # type: ignore[arg-type]
            report.append(f"deleted namespace gc-{APP}")
        for run_id in run_ids:
            await sandbox.cleanup(run_id)
        print("\n".join(["", *report]))


async def _run(sf: SessionFactory, run_id: uuid.UUID) -> Run:
    async with sf() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        return run
