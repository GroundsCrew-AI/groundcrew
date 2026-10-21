"""Per-workspace quotas (app.quotas) and the spend caps' rules: limits per workspace, daily
counts that cannot overshoot, clear 429s, and usage in Settings."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app import quotas
from app.config import get_settings
from app.db.models import Incident, Run, RunStatus, Workspace
from app.db.session import SessionFactory, get_session_factory
from app.llm.budget import BudgetExceededError, PgUsageStore, RunBudget, workspace_daily_cap
from app.main import app

from .test_api import Member, _member, _settings

pytestmark = pytest.mark.anyio


@pytest.fixture
async def client(session_factory: SessionFactory) -> AsyncIterator[httpx.AsyncClient]:
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = lambda: _settings()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def alice(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 9501, "alice")


async def _override(sf: SessionFactory, ws: uuid.UUID, **limits: int) -> None:
    async with sf() as session:
        workspace = await session.get_one(Workspace, ws)
        workspace.settings = {**(workspace.settings or {}), "quotas": limits}
        await session.commit()


async def test_daily_counts_stop_at_the_limit_even_when_raced(
    session_factory: SessionFactory, alice: Member
) -> None:
    settings = _settings(quota_tavily_per_workspace_day=3)

    async def one() -> bool:
        async with session_factory() as session:
            try:
                await quotas.take(session, alice.workspace_id, "tavily", settings)
            except quotas.QuotaExceededError:
                return False
            await session.commit()
            return True

    results = await asyncio.gather(*(one() for _ in range(8)))
    assert results.count(True) == 3
    async with session_factory() as session:
        assert await quotas.used_today(session, alice.workspace_id, "tavily") == 3
        with pytest.raises(quotas.QuotaExceededError) as caught:
            await quotas.take(session, alice.workspace_id, "tavily", settings)
    assert caught.value.limit == 3 and caught.value.resets_at == quotas.next_reset()
    assert "limit of 3 web research calls today. It resets at 00:00 UTC." in str(caught.value)


async def test_an_operator_override_applies_to_one_workspace(
    session_factory: SessionFactory, alice: Member
) -> None:
    bob = await _member(session_factory, 9502, "bob")
    await _override(session_factory, alice.workspace_id, builds=1)
    s = _settings(quota_builds_per_workspace_day=5)
    async with session_factory() as session:
        await quotas.take(session, alice.workspace_id, "builds", s)
        with pytest.raises(quotas.QuotaExceededError):
            await quotas.take(session, alice.workspace_id, "builds", s)
        for _ in range(5):
            await quotas.take(session, bob.workspace_id, "builds", s)
        await session.commit()


async def test_runs_people_start_are_limited_but_the_watcher_is_not(
    client: httpx.AsyncClient, session_factory: SessionFactory, alice: Member
) -> None:
    ws = alice.workspace_id
    body = {"agent": "echo_ops", "goal": "x", "workspace_id": str(ws)}
    for _ in range(2):
        assert (await client.post("/runs", json=body, headers=alice.headers)).status_code == 202
    r = await client.post("/runs", json=body, headers=alice.headers)
    assert r.status_code == 429
    assert r.json()["code"] == "quota_exceeded" and r.json()["quota"] == "concurrent_runs"
    assert r.json()["detail"] == "This workspace reached its limit of 2 runs going at once."
    assert "retry-after" not in r.headers  # not a daily count

    # The watcher's investigations do not count and are not held back.
    async with session_factory() as session:
        incident = Incident(
            workspace_id=ws, title="t", status="investigating", detected_by="watcher"
        )
        session.add(incident)
        await session.flush()
        session.add(
            Run(workspace_id=ws, agent="medic", goal="g", incident_id=incident.id,
                status=RunStatus.running)
        )  # fmt: skip
        await session.commit()
        assert await quotas.active_runs(session, ws) == 2

    # Finishing one frees a slot.
    async with session_factory() as session:
        run = await session.scalar(select(Run).where(Run.agent == "echo_ops").limit(1))
        assert run is not None
        run.status = RunStatus.succeeded
        await session.commit()
    assert (await client.post("/runs", json=body, headers=alice.headers)).status_code == 202


async def test_connections_are_limited(
    client: httpx.AsyncClient, session_factory: SessionFactory, alice: Member
) -> None:
    await _override(session_factory, alice.workspace_id, connections=0)
    r = await client.post(
        "/connections/vercel",
        json={"workspace_id": str(alice.workspace_id), "token": "t" * 24},
        headers=alice.headers,
    )
    assert r.status_code == 429 and r.json()["quota"] == "connections"


async def test_daily_quota_429_says_when_it_resets(
    client: httpx.AsyncClient, session_factory: SessionFactory, alice: Member
) -> None:
    @app.get("/_quota_probe")
    async def probe() -> None:
        raise quotas.QuotaExceededError("builds", 4, quotas.next_reset())

    r = await client.get("/_quota_probe")
    app.router.routes.pop()
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert r.json()["resets_at"] == quotas.next_reset().isoformat()


async def test_usage_shows_what_the_workspace_used(
    client: httpx.AsyncClient, session_factory: SessionFactory, alice: Member
) -> None:
    async with session_factory() as session:
        await quotas.take(session, alice.workspace_id, "tavily", _settings())
        await session.commit()
    await PgUsageStore(session_factory, alice.workspace_id).add(Decimal("0.12"), 100)
    r = await client.get(f"/workspaces/{alice.workspace_id}/usage", headers=alice.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["usd_today"] == pytest.approx(0.12) and body["platform_paused"] is False
    assert body["max_build_seconds"] == 900
    by_name = {q["name"]: q for q in body["quotas"]}
    assert by_name["tavily"] == {
        "name": "tavily",
        "label": "Web research calls today",
        "used": 1,
        "limit": 10,
        "daily": True,
    }
    assert by_name["concurrent_runs"]["daily"] is False


async def test_an_owner_can_lower_the_daily_cap_but_not_raise_it(
    client: httpx.AsyncClient, session_factory: SessionFactory, alice: Member
) -> None:
    s = _settings(llm_workspace_daily_usd_cap=0.5)
    for own, expected in ((5.0, "0.5"), (0.2, "0.2")):
        await client.patch(
            f"/workspaces/{alice.workspace_id}",
            json={"settings": {"daily_usd_cap": own}},
            headers=alice.headers,
        )
        async with session_factory() as session:
            assert await workspace_daily_cap(session, alice.workspace_id, s) == Decimal(expected)


async def test_spend_limits_read_as_plain_messages(session_factory: SessionFactory) -> None:
    store = PgUsageStore(session_factory)
    await store.add(Decimal("3"), 10)
    with pytest.raises(BudgetExceededError) as caught:
        await RunBudget(usage=store, settings=_settings(llm_daily_usd_cap=2.0)).check_usage()
    assert caught.value.cap == "daily_usd_cap"
    assert "It resets at 00:00 UTC." in caught.value.detail
    assert "LLM_" not in caught.value.detail
    with pytest.raises(BudgetExceededError) as caught:
        await RunBudget(usage=store, settings=_settings(llm_total_usd_cap=1.0)).check_usage()
    assert caught.value.cap == "total_usd_cap" and "paused" in caught.value.detail


def test_build_time_is_the_lower_of_both_limits() -> None:
    assert _settings(quota_max_build_seconds=600).build_timeout_seconds == 600
    assert _settings(sandbox_build_timeout_seconds=300).build_timeout_seconds == 300
