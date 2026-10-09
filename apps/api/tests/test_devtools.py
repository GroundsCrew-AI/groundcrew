"""Dev-only tools refuse to run against anything but a local dev instance."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import Settings
from app.devtools import NotDevError, require_dev

ROOT = Path(__file__).resolve().parents[3]


def _settings(env: str) -> Settings:
    return Settings(_env_file=None, app_env=env)


def test_app_env_defaults_to_production() -> None:
    assert Settings(_env_file=None).app_env == "production"


@pytest.mark.parametrize("env", ["production", "staging", "test"])
def test_refuses_outside_dev(env: str) -> None:
    with pytest.raises(NotDevError, match="APP_ENV"):
        require_dev(_settings(env), "http://localhost:8000", tool="make kind-register")


@pytest.mark.parametrize(
    "url", ["https://groundcrew.example.com", "http://10.0.0.5:8000", "http://api:8000", "http://"]
)
def test_refuses_an_api_that_is_not_on_this_machine(url: str) -> None:
    with pytest.raises(NotDevError, match="on this machine"):
        require_dev(_settings("dev"), url, tool="make kind-register")


@pytest.mark.parametrize(
    "url",
    ["http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000", "http://api.localhost"],
)
def test_allows_local_dev(url: str) -> None:
    require_dev(_settings("dev"), url, tool="make kind-register")


@pytest.mark.parametrize(
    "env,api,message",
    [
        ("production", "http://localhost:8000", "APP_ENV is 'production'"),
        ("dev", "https://groundcrew.example.com", "only talks to an API on this machine"),
    ],
)
def test_kind_register_refuses_before_signing_anything(env: str, api: str, message: str) -> None:
    """The script stops at the guard: no database, no kubeconfig, no token, no request."""
    result = subprocess.run(  # noqa: S603 (a fixed script in this repo)
        [sys.executable, str(ROOT / "scripts" / "kind_register.py"), "--login", "x", "--api", api],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={
            "PATH": os.environ["PATH"],
            "APP_ENV": env,
            # Unreachable on purpose: reaching the database would mean the guard did not stop it.
            "DATABASE_URL": "postgresql+asyncpg://nobody:x@db.invalid:1/none",
            "INTERNAL_JWT_SECRET": "x" * 40,
        },
    )
    assert result.returncode == 1
    assert message in result.stderr
    assert "Workspace:" not in result.stdout and "Traceback" not in result.stderr
