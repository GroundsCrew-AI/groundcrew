"""Groundcrew agents (app/remote_agents): adding one, enrolling with the join token, signed
requests (bad signature, replay, clock skew, removed connection), and the inventory reaching the
Map through a discovery of the connection."""

import base64
import json
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import select

from app.config import get_settings
from app.db.models import (
    Agent,
    AuditEvent,
    Connection,
    Resource,
    Run,
    WorkspaceMember,
    WorkspaceRole,
)
from app.db.session import SessionFactory, get_session_factory
from app.discovery.engine import DiscoveryEngine
from app.main import app
from app.remote_agents.signing import canonical
from app.security.ratelimit import limiter
from tests.test_api import Member, _member, _settings

pytestmark = pytest.mark.anyio

INVENTORY: dict[str, Any] = {
    "host": {"hostname": "web-1", "os": "linux", "arch": "amd64", "distribution": "Ubuntu 24.04"},
    "services": [
        {
            "name": "legacy-flask-app",
            "runtime": "docker",
            "target": "flask-app",
            "image": "ghcr.io/acme/flask-app:af37",
            "image_digest": "sha256:" + "a" * 64,
            "ports": ["8000/tcp"],
            "health_url": "http://127.0.0.1:8000/health",
            "logs": "container",
            "allow": ["restart"],
        },
        {"name": "nginx", "runtime": "systemd", "target": "nginx.service"},
    ],
}


@pytest.fixture
async def client(session_factory: SessionFactory) -> AsyncIterator[httpx.AsyncClient]:
    limiter.reset()
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = lambda: _settings(
        agent_api_url="https://api.example.test"
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


class FakeAgent:
    """Signs requests the way apps/agent does."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.key = Ed25519PrivateKey.generate()
        self.id: str | None = None

    @property
    def public_key(self) -> str:
        raw = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return base64.b64encode(raw).decode()

    async def enroll(self, token: str) -> httpx.Response:
        r = await self.client.post(
            "/agent/v1/enroll",
            json={
                "token": token,
                "public_key": self.public_key,
                "hostname": "web-1",
                "os": "linux",
                "arch": "amd64",
                "version": "0.1.0",
            },
        )
        if r.status_code == 200:
            self.id = r.json()["agent_id"]
        return r

    def headers(
        self, path: str, body: bytes, *, nonce: str | None = None, at: int | None = None
    ) -> dict[str, str]:
        stamp, nonce = str(at or int(time.time())), nonce or secrets.token_urlsafe(16)
        signature = self.key.sign(canonical("POST", path, stamp, nonce, body))
        return {
            "content-type": "application/json",
            "x-groundcrew-agent": str(self.id),
            "x-groundcrew-timestamp": stamp,
            "x-groundcrew-nonce": nonce,
            "x-groundcrew-signature": base64.b64encode(signature).decode(),
        }

    async def post(self, path: str, payload: dict[str, Any], **kw: Any) -> httpx.Response:
        body = json.dumps(payload).encode()
        return await self.client.post(path, content=body, headers=self.headers(path, body, **kw))


async def _add(client: httpx.AsyncClient, owner: Member) -> dict[str, Any]:
    r = await client.post(
        "/connections/agent",
        json={"workspace_id": str(owner.workspace_id), "name": "web-1"},
        headers=owner.headers,
    )
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def _enrolled(client: httpx.AsyncClient, owner: Member) -> tuple[FakeAgent, dict[str, Any]]:
    added = await _add(client, owner)
    agent = FakeAgent(client)
    assert (await agent.enroll(added["token"])).status_code == 200
    return agent, added


def test_signatures_match_the_go_agent_vector() -> None:
    """The same vector is checked by apps/agent/internal/client/client_test.go."""
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    assert base64.b64encode(pub).decode() == "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg="
    message = canonical(
        "POST",
        "/agent/v1/heartbeat",
        "1700000000",
        "nonce-nonce-nonce-1",
        b'{"version":"0.1.0","uptime_seconds":5}',
    )
    assert base64.b64encode(key.sign(message)).decode() == (
        "ze/AbEhegKTHPcNFp74SbEHNdPREcXDOjHtKa+25RLJ+wc5ccTOsyCek7T2we7uC5iEDLTB2JWMK0dz8yvE5BQ=="
    )


async def test_adding_an_agent_returns_a_one_time_token_and_keeps_only_its_hash(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    alice = await _member(session_factory, 7001, "alice")
    added = await _add(client, alice)
    token = added["token"]
    assert token.startswith("gcj_") and added["api_url"] == "https://api.example.test"
    conn = added["connection"]
    assert conn["kind"] == "agent" and conn["status"] == "pending"
    assert conn["agent"]["enrolled"] is False and conn["agent"]["online"] is False
    assert conn["agent"]["join_expires_at"] is not None
    async with session_factory() as session:
        agent = await session.scalar(select(Agent))
        assert agent is not None and agent.join_token_hash is not None
        assert token not in (agent.join_token_hash or "")


async def test_a_viewer_cannot_add_an_agent(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    alice = await _member(session_factory, 7002, "alice")
    viewer = await _member(session_factory, 7003, "vic")
    async with session_factory() as session:
        session.add(
            WorkspaceMember(
                workspace_id=alice.workspace_id, user_id=viewer.user_id, role=WorkspaceRole.viewer
            )
        )
        await session.commit()
    r = await client.post(
        "/connections/agent",
        json={"workspace_id": str(alice.workspace_id), "name": "web-1"},
        headers=viewer.headers,
    )
    assert r.status_code == 403


async def test_enrolling_connects_once_and_the_token_cannot_be_reused(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    alice = await _member(session_factory, 7004, "alice")
    _, added = await _enrolled(client, alice)
    r = await client.get(
        "/connections", params={"workspace_id": str(alice.workspace_id)}, headers=alice.headers
    )
    (conn,) = r.json()
    assert conn["status"] == "connected" and conn["account"] is None
    assert conn["agent"]["enrolled"] and conn["agent"]["online"]
    assert conn["agent"]["hostname"] == "web-1" and conn["agent"]["join_expires_at"] is None
    assert any("connects out to Groundcrew" in line for line in conn["access"])

    again = await FakeAgent(client).enroll(added["token"])
    assert again.status_code == 401 and "not valid" in again.json()["detail"]
    async with session_factory() as session:
        actions = list(await session.scalars(select(AuditEvent.action)))
    assert "connection.agent_enrolled" in actions


async def test_an_expired_token_is_refused(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    alice = await _member(session_factory, 7005, "alice")
    added = await _add(client, alice)
    async with session_factory() as session:
        agent = await session.scalar(select(Agent))
        assert agent is not None
        agent.join_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    assert (await FakeAgent(client).enroll(added["token"])).status_code == 401


async def test_a_new_token_reinstalls_the_agent_with_a_new_key(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    alice = await _member(session_factory, 7006, "alice")
    old, added = await _enrolled(client, alice)
    conn_id = added["connection"]["id"]
    r = await client.post(f"/connections/{conn_id}/agent/token", headers=alice.headers)
    assert r.status_code == 200
    new = FakeAgent(client)
    assert (await new.enroll(r.json()["token"])).status_code == 200
    assert new.id == old.id  # the same agent, with the new key
    assert (
        await old.post("/agent/v1/heartbeat", {"version": "0.1.0", "uptime_seconds": 1})
    ).status_code == 401
    assert (
        await new.post("/agent/v1/heartbeat", {"version": "0.1.0", "uptime_seconds": 1})
    ).status_code == 200


async def test_signed_requests_refuse_bad_signatures_replays_and_clock_skew(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    alice = await _member(session_factory, 7007, "alice")
    agent, _ = await _enrolled(client, alice)
    beat = {"version": "0.1.1", "uptime_seconds": 30}
    ok = await agent.post("/agent/v1/heartbeat", beat, nonce="n" * 20)
    assert ok.status_code == 200, ok.text
    replay = await agent.post("/agent/v1/heartbeat", beat, nonce="n" * 20)
    assert replay.status_code == 401 and "replayed" in replay.json()["detail"]
    skewed = await agent.post("/agent/v1/heartbeat", beat, at=int(time.time()) - 600)
    assert skewed.status_code == 401 and "clock" in skewed.json()["detail"]

    body = json.dumps(beat).encode()
    headers = agent.headers("/agent/v1/heartbeat", body)
    tampered = await client.post(
        "/agent/v1/heartbeat", content=json.dumps({**beat, "version": "9"}), headers=headers
    )
    assert tampered.status_code == 401 and "signature" in tampered.json()["detail"]
    unsigned = await client.post("/agent/v1/heartbeat", json=beat)
    assert unsigned.status_code == 401
    async with session_factory() as session:
        stored = await session.scalar(select(Agent))
        assert stored is not None and stored.version == "0.1.1"


async def test_removing_the_connection_stops_the_agent_at_once(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    alice = await _member(session_factory, 7008, "alice")
    agent, added = await _enrolled(client, alice)
    r = await client.delete(f"/connections/{added['connection']['id']}", headers=alice.headers)
    assert r.status_code in (200, 204), r.text
    beat = await agent.post("/agent/v1/heartbeat", {"version": "0.1.0", "uptime_seconds": 1})
    assert beat.status_code == 401


async def test_inventory_reaches_the_map_through_a_discovery(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    alice = await _member(session_factory, 7009, "alice")
    agent, added = await _enrolled(client, alice)
    r = await agent.post("/agent/v1/inventory", INVENTORY)
    assert r.status_code == 200, r.text
    run_id = r.json()["discovery_run_id"]
    assert r.json()["changed"] is True
    await DiscoveryEngine(session_factory).run(
        uuid.UUID(run_id), connection_id=uuid.UUID(added["connection"]["id"])
    )
    async with session_factory() as session:
        run = await session.get(Run, uuid.UUID(run_id))
        assert run is not None and run.status == "succeeded", run.error if run else None
        found = {
            r.name: r
            for r in await session.scalars(select(Resource).where(Resource.provider == "agent"))
        }
    assert set(found) == {"web-1", "legacy-flask-app", "nginx"}
    assert found["web-1"].type == "agent:host"
    flask = found["legacy-flask-app"]
    assert flask.type == "agent:service" and flask.account == "web-1"
    assert flask.config["runtime"] == "docker" and flask.config["allow"] == ["restart"]

    same = await agent.post("/agent/v1/inventory", INVENTORY)
    assert same.json() == {"changed": False, "discovery_run_id": None}
    beat = await agent.post("/agent/v1/heartbeat", {"version": "0.1.0", "uptime_seconds": 9})
    assert beat.json()["inventory_hash"] is not None

    # A service removed from the file goes stale on the next discovery.
    fewer = {**INVENTORY, "services": INVENTORY["services"][:1]}
    r = await agent.post("/agent/v1/inventory", fewer)
    await DiscoveryEngine(session_factory).run(
        uuid.UUID(r.json()["discovery_run_id"]),
        connection_id=uuid.UUID(added["connection"]["id"]),
    )
    async with session_factory() as session:
        nginx = await session.scalar(select(Resource).where(Resource.name == "nginx"))
        assert nginx is not None and nginx.stale


async def test_inventory_refuses_unknown_fields(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    alice = await _member(session_factory, 7010, "alice")
    agent, _ = await _enrolled(client, alice)
    bad = {**INVENTORY, "services": [{**INVENTORY["services"][0], "env": {"PASSWORD": "x"}}]}
    assert (await agent.post("/agent/v1/inventory", bad)).status_code == 422
    async with session_factory() as session:
        conn = await session.scalar(select(Connection))
        assert conn is not None and "inventory" not in (conn.config or {})
