"""Shared fixtures. Tests use a real Postgres (TEST_DATABASE_URL) and never call Token Factory.

Local: `make db` starts the compose Postgres; the test database is created if missing.
CI: a pgvector service container provides it.
"""

import asyncio
import functools
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://groundcrew:groundcrew@localhost:5433/groundcrew_test",
)

# Tests must not depend on a developer's .env, and must never reach Token Factory.
Settings.model_config["env_file"] = None
# Like dev, minus dev-only tools; a test that needs production or dev passes app_env itself.
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["TOKEN_FACTORY_BASE_URL"] = "http://token-factory.invalid/v1"  # noqa: S105
os.environ["TOKEN_FACTORY_API_KEY"] = "test-key-not-real"  # noqa: S105
os.environ["MODEL_NANO"] = "test/nano"
os.environ["MODEL_SUPER"] = "test/super"
os.environ["MODEL_ULTRA"] = "test/ultra"
os.environ["INTERNAL_JWT_SECRET"] = "test-internal-jwt-secret-0123456789abcdef"  # noqa: S105
os.environ["APPROVAL_TOKEN_KEY"] = "test-approval-token-key-0123456789abcdef"  # noqa: S105
# Fake Kubernetes API servers run on 127.0.0.1; tests of the private-address rule pass
# allow_private=False explicitly.
os.environ["K8S_ALLOW_PRIVATE_SERVERS"] = "true"

from app.db.models import Base, User, Workspace, WorkspaceMember, WorkspaceRole  # noqa: E402
from app.db.session import SessionFactory  # noqa: E402


@functools.cache
def docker_available() -> bool:
    """True when a Docker daemon answers (`docker info`), checked once per session."""
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],  # noqa: S607
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """`live` tests run only with RUN_LIVE_TESTS=1; `docker` tests only with a daemon."""
    live = os.environ.get("RUN_LIVE_TESTS") == "1"
    for item in items:
        if "live" in item.keywords and not live:
            item.add_marker(pytest.mark.skip(reason="live test: set RUN_LIVE_TESTS=1"))
        elif "docker" in item.keywords and not docker_available():
            item.add_marker(pytest.mark.skip(reason="no Docker daemon (docker info failed)"))


API_DIR = Path(__file__).resolve().parents[1]


async def _create_database_if_missing(url: str) -> None:
    parsed = make_url(url)
    conn = await asyncpg.connect(
        host=parsed.host,
        port=parsed.port,
        user=parsed.username,
        password=parsed.password,
        database="postgres",
    )
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", parsed.database
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{parsed.database}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def database_url() -> str:
    asyncio.run(_create_database_if_missing(TEST_DATABASE_URL))
    config = Config(str(API_DIR / "alembic.ini"))
    config.attributes["database_url"] = TEST_DATABASE_URL
    command.upgrade(config, "head")
    return TEST_DATABASE_URL


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    # NullPool: connections never outlive the test's event loop.
    engine = create_async_engine(database_url, poolclass=NullPool)
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} CASCADE"))
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def workspace(session_factory: SessionFactory) -> Workspace:
    async with session_factory() as session:
        user = User(email="test@groundcrew.local", name="Test")
        session.add(user)
        await session.flush()
        ws = Workspace(name="Test", owner_id=user.id)
        session.add(ws)
        await session.flush()
        # Like every real workspace: its creator is an owner member.
        session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceRole.owner))
        await session.commit()
        return ws
