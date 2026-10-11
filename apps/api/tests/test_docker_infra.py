"""The build daemon's configuration, checked with a real Docker daemon (marker `docker`:
skipped when there is none). The offline checks are in tests/test_compose_dind.py."""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.docker


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_dind_accepts_its_daemon_json() -> None:
    """dockerd --validate, in the pinned dind image: daemon.json would not stop dind at start."""
    compose = (ROOT / "docker-compose.yml").read_text()
    image = re.search(r"image: (docker:[\d.]+-dind)", compose)
    assert image
    config = ROOT / "infra" / "dind" / "daemon.json"
    result = _docker("run", "--rm", "-v", f"{config}:/etc/docker/daemon.json:ro",
                     "--entrypoint", "dockerd", image.group(1),
                     "--validate", "--config-file", "/etc/docker/daemon.json")  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert "configuration OK" in result.stdout + result.stderr


@pytest.mark.parametrize(
    "files", [["docker-compose.yml"], ["docker-compose.yml", "docker-compose.kind.yml"]]
)
def test_compose_files_are_valid(files: list[str]) -> None:
    args = [a for f in files for a in ("-f", str(ROOT / f))]
    result = _docker("compose", *args, "config", "--quiet")
    assert result.returncode == 0, result.stderr
