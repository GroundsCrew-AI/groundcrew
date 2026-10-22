"""Platform basics: production config guard, readiness, job pruning, the watcher re-arm."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, update

from app import queue
from app.config import Settings
from app.db.models import Job, JobStatus
from app.db.session import SessionFactory
from app.incidents.watcher import WATCH_JOB
from app.main import app
from app.worker import maintenance_pass

pytestmark = pytest.mark.anyio

SECRET = "x" * 40


def _prod(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "production",
        "database_url": SecretStr("postgresql+asyncpg://u:p@db/gc"),
        "secrets_key": SecretStr(SECRET),
        "internal_jwt_secret": SecretStr(SECRET),
        "approval_token_key": SecretStr(SECRET),
        "web_origin": "https://app.example.com",
        "agent_api_url": "https://api.example.com",
        "smtp_host": "smtp.example.com",
        # conftest turns it on for the fake API servers on 127.0.0.1.
        "k8s_allow_private_servers": None,
        **overrides,
    }
    return Settings(_env_file=None, **values)


def test_production_needs_its_secrets_and_a_real_origin() -> None:
    assert _prod().production_problems() == []
    bad = _prod(secrets_key=None, internal_jwt_secret=SecretStr("short"),
                web_origin="http://localhost:3000",
                agent_api_url="http://localhost:8000")  # fmt: skip
    assert bad.production_problems() == [
        "SECRETS_KEY is not set",
        "INTERNAL_JWT_SECRET is shorter than 32 characters",
        "WEB_ORIGIN still points at localhost",
        "AGENT_API_URL still points at localhost",
    ]
    with pytest.raises(RuntimeError, match="APP_ENV=production cannot start"):
        bad.require_production_ready()
    # Email sign-up needs a way to send its links, unless it is off.
    assert any(
        "SMTP_HOST" in p
        for p in _prod(smtp_host=None, password_auth_enabled=True).production_problems()
    )
    assert any("sign-in links" in p for p in _prod(smtp_host=None).production_problems())
    assert _prod(smtp_host=None, email_link_auth_enabled=False).production_problems() == []
    # Dev and test run without them.
    Settings(_env_file=None, app_env="dev").require_production_ready()


async def test_ready_checks_the_database_and_migrations() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/ready")
    assert r.status_code == 200, r.text
    assert r.json()["database"] == "ok" and r.json()["migrations"] == "ok"


async def test_prune_deletes_only_old_finished_jobs(session_factory: SessionFactory) -> None:
    old = datetime.now(UTC) - timedelta(days=40)
    async with session_factory() as session:
        for status in (JobStatus.done, JobStatus.failed, JobStatus.queued, JobStatus.done):
            await queue.enqueue(session, f"k-{status}", {})
        await session.commit()
        ids = list(await session.scalars(select(Job.id).order_by(Job.created_at)))
        await session.execute(
            update(Job).where(Job.id == ids[0]).values(status=JobStatus.done, updated_at=old)
        )
        await session.execute(
            update(Job).where(Job.id == ids[1]).values(status=JobStatus.failed, updated_at=old)
        )
        await session.execute(update(Job).where(Job.id == ids[2]).values(updated_at=old))
        await session.execute(update(Job).where(Job.id == ids[3]).values(status=JobStatus.done))
        await session.commit()
        removed = await queue.prune(session, done_older_than_days=7, failed_older_than_days=30)
        left = set(await session.scalars(select(Job.id)))
    assert removed == 2 and left == {ids[2], ids[3]}


async def test_maintenance_rearms_the_watcher(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.worker.get_settings", lambda: Settings(_env_file=None, incident_watch_enabled=True)
    )
    await maintenance_pass(session_factory)
    await maintenance_pass(session_factory)  # idempotent: one queued watch job
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count(Job.id)).where(Job.kind == WATCH_JOB, Job.status == JobStatus.queued)
        )
    assert count == 1


def test_the_worker_heartbeat_file(tmp_path: Any) -> None:
    from app.worker import touch_heartbeat

    beat = tmp_path / "beat"
    touch_heartbeat(None)
    touch_heartbeat(beat)
    assert beat.exists()
    touch_heartbeat(tmp_path / "missing-dir" / "beat")  # never raises
