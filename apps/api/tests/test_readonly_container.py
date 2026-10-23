"""The API image works the way production runs it: read-only root filesystem, no capabilities,
the worker's uid, and only the tmpfs mounts from infra/platform/host/compose.yml writable
(marker `docker`: skipped when there is no daemon). A repository clone, a Dockerfile written
next to it, and a temporary Docker client config all land in TMPDIR; without the tmpfs the
startup check refuses to run."""

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
IMAGE = "groundcrew/api:readonly-test"
pytestmark = pytest.mark.docker

# Runs inside the container: the startup check, then the same calls a deploy makes before Scout.
SCRIPT = """
import os, subprocess, tempfile
from pathlib import Path
from app.config import get_settings
from app.repos.source import GitCloneSource
from app.writable import check_writable

check_writable(get_settings())
src = Path(tempfile.mkdtemp(prefix="origin-"))
env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
(src / "app.py").write_text("print('hi')\\n")
for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "first"]):
    subprocess.run(["git", *args], cwd=src, env=env, check=True)
with GitCloneSource(f"file://{src}", ref="main", allowed_schemes=("file",)) as root:
    assert (root / "app.py").exists(), root
    (root / "Dockerfile").write_text("FROM scratch\\n")  # what Builder does
    assert str(root).startswith(os.environ["TMPDIR"]), root
with tempfile.TemporaryDirectory(prefix="groundcrew-docker-") as config:  # registry login
    (Path(config) / "config.json").write_text("{}")
print("ok", os.getuid(), root)
"""


def _docker(*args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@pytest.fixture(scope="module")
def image() -> str:
    built = _docker("build", "-q", "-t", IMAGE, str(ROOT / "apps" / "api"), timeout=900)
    assert built.returncode == 0, built.stderr
    return IMAGE


def _worker() -> dict[str, Any]:
    compose = yaml.safe_load((ROOT / "infra" / "platform" / "host" / "compose.yml").read_text())
    worker: dict[str, Any] = compose["services"]["worker"]
    return worker


def _run(image: str, *, tmpfs: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    worker = _worker()
    args = ["run", "--rm", "--read-only", "--cap-drop", "ALL"]
    args += ["--security-opt", "no-new-privileges", "--user", str(worker["user"])]
    args += [a for t in tmpfs for a in ("--tmpfs", t)]
    args += [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
    return _docker(*args, image, "python", "-c", SCRIPT, timeout=120)


def test_production_mounts_make_temp_files_work(image: str) -> None:
    worker = _worker()
    env = {k: str(worker["environment"][k]) for k in ("TMPDIR", "HOME")}
    done = _run(image, tmpfs=[str(t) for t in worker["tmpfs"]], env=env)
    assert done.returncode == 0, done.stdout + done.stderr
    uid = str(worker["user"]).split(":")[0]
    assert done.stdout.startswith(f"ok {uid} /scratch/groundcrew-repo-"), done.stdout


def test_without_the_tmpfs_the_worker_refuses_to_start(image: str) -> None:
    tmp = "/tmp:size=16m,mode=1777"  # noqa: S108 - a mount inside the container
    done = _run(image, tmpfs=[tmp], env={"TMPDIR": "/scratch"})
    assert done.returncode != 0
    assert "TMPDIR=/scratch is not writable by uid 10001" in done.stderr, done.stderr
