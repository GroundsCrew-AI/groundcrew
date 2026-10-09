"""Opt-in: runs `make kind-check` against the live kind cluster (RUN_LIVE_TESTS=1, after
make kind-up and make kind-kubeconfig). It checks the deployer's RBAC with kubectl auth can-i
and the admission policy with server-side dry runs, as the ServiceAccount itself."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
KUBECONFIG = ROOT / ".kind" / "groundcrew-deployer.host.kubeconfig"

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1", reason="live test: set RUN_LIVE_TESTS=1"
)


def test_deployer_cannot_leave_gc_namespaces() -> None:
    if not KUBECONFIG.exists():
        pytest.fail("No deployer kubeconfig: run make kind-up and make kind-kubeconfig first.")
    result = subprocess.run(  # noqa: S603 (a fixed script in this repo)
        [str(ROOT / "scripts" / "kind" / "check.sh")],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "All checks passed." in result.stderr
    assert "FAIL" not in result.stdout
