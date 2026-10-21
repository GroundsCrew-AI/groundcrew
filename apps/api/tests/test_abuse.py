"""Abuse protection in the API: per-user call, stream-token, and open-stream limits, request
size limits, sign-up throttling, and the operator's switch to pause sign-ups."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.admin_cli import main as admin_cli
from app.config import get_settings
from app.db.models import AuditEvent, Run, RunStatus, User
from app.db.session import SessionFactory, get_session_factory
from app.main import app
from app.security.ratelimit import streams

from .auth_helpers import service_headers
from .test_api import Member, _member, _settings

pytestmark = pytest.mark.anyio


def _use(**overrides: Any) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(**overrides)


@pytest.fixture
async def client(session_factory: SessionFactory) -> AsyncIterator[httpx.AsyncClient]:
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    _use()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def alice(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 9601, "alice")


async def test_each_user_has_a_call_limit(
    client: httpx.AsyncClient, session_factory: SessionFactory, alice: Member
) -> None:
    _use(rate_limit_user_per_minute=3)
    bob = await _member(session_factory, 9602, "bob")
    for _ in range(3):
        assert (await client.get("/me", headers=alice.headers)).status_code == 200
    r = await client.get("/me", headers=alice.headers)
    assert r.status_code == 429 and r.headers["retry-after"] == "60"
    # Someone else is not held back.
    assert (await client.get("/me", headers=bob.headers)).status_code == 200


async def test_stream_tokens_and_open_streams_are_limited(
    client: httpx.AsyncClient, session_factory: SessionFactory, alice: Member
) -> None:
    async with session_factory() as session:
        run = Run(workspace_id=alice.workspace_id, agent="echo_ops", goal="g",
                  status=RunStatus.succeeded)  # fmt: skip
        session.add(run)
        await session.commit()
    _use(rate_limit_stream_tokens_per_minute=2, max_open_streams_per_user=1)
    tokens = [await client.post(f"/runs/{run.id}/stream-token", headers=alice.headers)
              for _ in range(3)]  # fmt: skip
    assert [t.status_code for t in tokens] == [200, 200, 429]
    token = tokens[0].json()["token"]

    # One live view open already: the next is refused, and allowed again once it closes.
    assert streams.acquire(f"stream:{alice.user_id}", 1)
    r = await client.get(f"/runs/{run.id}/events", params={"token": token})
    assert r.status_code == 429 and "live views open" in r.json()["detail"]
    streams.release(f"stream:{alice.user_id}")
    r = await client.get(f"/runs/{run.id}/events", params={"token": token})
    assert r.status_code == 200 and "event: done" in r.text
    # The finished stream gave its slot back.
    assert streams.acquire(f"stream:{alice.user_id}", 1)


async def test_large_request_bodies_are_refused(client: httpx.AsyncClient, alice: Member) -> None:
    _use()
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    big = b'{"name": "' + b"x" * 2_000_000 + b'"}'
    headers = {**alice.headers, "content-type": "application/json"}
    r = await client.patch("/me", content=big, headers=headers)
    assert r.status_code == 413 and "larger than 1024 KB" in r.json()["detail"]

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(20):
            yield b"x" * 100_000

    r = await client.patch("/me", content=chunks(), headers=headers)
    assert r.status_code == 413


def _profile(gid: int, login: str, **extra: Any) -> dict[str, Any]:
    return {"provider": "github", "account_id": str(gid), "login": login,
            "email": f"{login}@example.com", "email_verified": True, **extra}  # fmt: skip


async def test_signups_can_be_paused_but_invites_and_existing_users_get_in(
    client: httpx.AsyncClient, session_factory: SessionFactory, alice: Member
) -> None:
    _use(platform_admins=["alice"])
    me = (await client.get("/me", headers=alice.headers)).json()
    assert me["platform_admin"] is True
    r = await client.put("/admin/platform/signups", json={"paused": True},
                         headers={**alice.headers, "x-client-ip": "203.0.113.9"})  # fmt: skip
    assert r.status_code == 200 and r.json()["signups_paused"] is True

    headers = service_headers()
    refused = await client.post("/auth/oauth", json=_profile(9610, "newbie"), headers=headers)
    assert refused.status_code == 403 and refused.json()["code"] == "signups_paused"
    async with session_factory() as session:
        assert await session.scalar(select(User).where(User.github_login == "newbie")) is None

    # Alice still signs in; someone she invited can join.
    assert (await client.post("/auth/oauth", json=_profile(9601, "alice"),
                              headers=headers)).status_code == 200  # fmt: skip
    await client.post(f"/workspaces/{alice.workspace_id}/members",
                      json={"github_login": "guest"}, headers=alice.headers)  # fmt: skip
    joined = await client.post("/auth/oauth", json=_profile(9611, "guest"), headers=headers)
    assert joined.status_code == 200

    async with session_factory() as session:
        event = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "platform.signups_paused")
        )
    assert event is not None and event.workspace_id is None and event.ip == "203.0.113.9"

    r = await client.put("/admin/platform/signups", json={"paused": False}, headers=alice.headers)
    assert r.json()["signups_paused"] is False
    assert (await client.post("/auth/oauth", json=_profile(9610, "newbie"),
                              headers=headers)).status_code == 200  # fmt: skip


async def test_platform_switches_are_hidden_from_everyone_else(
    client: httpx.AsyncClient, alice: Member
) -> None:
    _use(platform_admins=["someone-else"])
    assert (await client.get("/admin/platform", headers=alice.headers)).status_code == 404
    r = await client.put("/admin/platform/signups", json={"paused": True}, headers=alice.headers)
    assert r.status_code == 404
    assert (await client.get("/me", headers=alice.headers)).json()["platform_admin"] is False


async def test_new_accounts_are_throttled(client: httpx.AsyncClient) -> None:
    headers = service_headers()
    _use(signups_per_ip_day=2)
    ip = {**headers, "x-client-ip": "198.51.100.20"}
    codes = []
    for i in range(3):
        r = await client.post("/auth/oauth", json=_profile(9620 + i, f"u{i}"), headers=ip)
        codes.append(r.status_code)
    assert codes == [200, 200, 429]
    # Signing in again is not a sign-up.
    assert (
        await client.post("/auth/oauth", json=_profile(9620, "u0"), headers=ip)
    ).status_code == 200

    _use(signups_per_hour=2)  # u0 and u1 this hour
    other = {**headers, "x-client-ip": "198.51.100.21"}
    r = await client.post("/auth/oauth", json=_profile(9630, "late"), headers=other)
    assert r.status_code == 429 and r.json()["code"] == "signup_rate_limited"


def test_admin_cli_explains_its_usage() -> None:
    with pytest.raises(SystemExit) as caught:
        admin_cli(["nope"])
    assert "signups status|pause|resume" in str(caught.value)
