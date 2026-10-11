"""Opt-in checks against the live kind cluster (RUN_LIVE_TESTS=1, after make kind-up and
make kind-kubeconfig).

- the deployer's limits: `make kind-check` (kubectl auth can-i for RBAC, server-side dry runs
  for the admission policy, as the ServiceAccount itself)
- CredentialsProvider.kubeconfig: a 0600 file that reaches the cluster, removed on close
- LiveKubeReader and discovery on the real cluster: namespaces, workloads, routes, no Secrets
- with KIND_LOGIN=<GitHub login> and `make dev KIND=1` running: register through the API, run
  discovery, and see the cluster's gc-* namespaces on the Map (make kind-register DISCOVER=1)
"""

import os
import shutil
import stat
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config import get_settings
from app.connectors.credentials import ConnectionCredentialsProvider
from app.connectors.kubernetes import LiveKubeReader
from app.db.models import Connection
from app.discovery.kubernetes import discover_kubernetes
from app.graph.edges import infer_edges, key
from app.security.crypto import encrypt, generate_key

ROOT = Path(__file__).resolve().parents[3]
HOST_KUBECONFIG = ROOT / ".kind" / "groundcrew-deployer.host.kubeconfig"
KUBECTL = ROOT / ".tools" / "bin" / "kubectl"

pytestmark = [pytest.mark.live, pytest.mark.anyio]


@pytest.fixture
def kubeconfig() -> str:
    if not HOST_KUBECONFIG.exists():
        pytest.fail("No deployer kubeconfig: run make kind-up and make kind-kubeconfig first.")
    return HOST_KUBECONFIG.read_text()


@pytest.fixture
def secrets_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SECRETS_KEY", generate_key())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# The dev stack's database (make dev), not the test database conftest points DATABASE_URL at.
DEV_DATABASE_URL = os.environ.get(
    "KIND_DATABASE_URL", "postgresql+asyncpg://groundcrew:groundcrew@localhost:5433/groundcrew"
)


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 (fixed commands from this repo)
        list(args),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=ROOT,
        env=env or None,
    )


def test_deployer_cannot_leave_gc_namespaces(kubeconfig: str) -> None:
    result = _run(str(ROOT / "scripts" / "kind" / "check.sh"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "All checks passed." in result.stderr
    assert "FAIL" not in result.stdout


async def test_provider_kubeconfig_is_private_and_reaches_the_cluster(
    kubeconfig: str, secrets_key: None
) -> None:
    conn = Connection(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        kind="k8s",
        name="kind groundcrew",
        status="connected",
        config={"cluster": "groundcrew"},
        secret_encrypted=encrypt(kubeconfig),
    )
    with ConnectionCredentialsProvider([conn]) as provider:
        path = provider.kubeconfig(conn.id)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        kubectl = str(KUBECTL) if KUBECTL.exists() else shutil.which("kubectl")
        assert kubectl, "kubectl not found (make kind-up downloads it into .tools/bin)"
        got = _run(kubectl, "--kubeconfig", str(path), "get", "namespaces", "-o", "name")
        assert got.returncode == 0, got.stderr
        assert "namespace/groundcrew-system" in got.stdout
        reader = LiveKubeReader(path.read_text())
        try:
            assert "groundcrew-system" in await reader.verify()
        finally:
            reader.close()
    assert not path.exists()


async def test_live_discovery_reads_the_cluster_without_secrets(kubeconfig: str) -> None:
    reader = LiveKubeReader(kubeconfig)
    try:
        result = await discover_kubernetes(reader, "groundcrew")
    finally:
        reader.close()
    assert "secrets" not in reader.calls
    names = {(r.type, r.name) for r in result.resources}
    assert ("k8s:namespace", "groundcrew-system") in names
    assert ("k8s:deployment", "ingress-nginx-controller") in names
    assert not any(n.startswith("kube-") for t, n in names if t == "k8s:namespace")
    by_key = {key(r): r for r in result.resources}
    routes = {
        (by_key[e.source].name, by_key[e.target].name)
        for e in infer_edges(result.resources, []).edges
    }
    assert ("ingress-nginx-controller", "ingress-nginx-controller") in routes  # svc -> deploy
    for r in result.resources:
        for c in r.config.get("containers") or []:
            assert set(c["env_values"]) <= set(c["env_names"])


def test_register_through_the_api_and_see_gc_namespaces_on_the_map(kubeconfig: str) -> None:
    login = os.environ.get("KIND_LOGIN")
    if not login:
        pytest.skip("set KIND_LOGIN=<GitHub login> with make dev KIND=1 running")
    result = _run(
        "uv", "run", "--project", "apps/api", "python", "scripts/kind_register.py",
        "--login", login, "--discover",
        # A clean environment: conftest overrides INTERNAL_JWT_SECRET and others, and the
        # script must use the stack's real .env, as make kind-register does.
        env={
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", ""),
            "DATABASE_URL": DEV_DATABASE_URL,
        },
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Live check passed." in result.stdout
