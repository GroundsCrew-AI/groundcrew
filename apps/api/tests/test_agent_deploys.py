"""Deploys through a Groundcrew agent (app/targets/agent.py): the plan names the Groundcrew
registry (or the connection's own), preflight checks the agent and the service's `allow`, apply
uploads the image and sends a signed deploy command whose registry login and env values the
agent fetches once, and POST /deploys refuses a service that does not allow deploy."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app import registry
from app.config import get_settings
from app.db.models import AgentCommand, Connection, Workspace
from app.db.session import SessionFactory, get_session_factory
from app.main import app
from app.pipeline.fake import ScriptedSandbox
from app.security.crypto import generate_key
from app.security.ratelimit import limiter
from app.targets.agent import AgentTarget
from app.targets.base import DeploySpec, EnvValue
from tests.target_fakes import approved_action
from tests.test_agent_signals import FLASK, _agent_with_services, state
from tests.test_api import _member, _settings

pytestmark = pytest.mark.anyio

REGISTRY = {"registry_internal_url": "http://registry:5000", "registry_public_host": "api.gc.test"}


@pytest.fixture(autouse=True)
def secrets_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Deploy secrets and registry tokens are stored encrypted (SECRETS_KEY)."""
    monkeypatch.setenv("SECRETS_KEY", generate_key())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client(session_factory: SessionFactory) -> AsyncIterator[httpx.AsyncClient]:
    limiter.reset()
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = lambda: _settings(**REGISTRY)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c
    app.dependency_overrides.clear()


async def _conn(sf: SessionFactory, conn_id: uuid.UUID) -> Connection:
    async with sf() as session:
        conn = await session.get(Connection, conn_id)
        assert conn is not None
        return conn


def _spec(conn: Connection, run_id: uuid.UUID, service: str = FLASK) -> DeploySpec:
    return DeploySpec(
        run_id=run_id,
        connection_id=conn.id,
        app="Flask App",
        source_image="gcb-run/flask:abc123def456",
        source_image_id="sha256:" + "c" * 64,
        smoke_passed=True,
        tag="abc123def456",
        port=8000,
        health_path="/health",
        env=[
            EnvValue(name="DATABASE_URL", value=SecretStr("postgres://db/app"), secret=True),
            EnvValue(name="LOG_LEVEL", value=SecretStr("info")),
        ],
        target_service=service,
    )


async def test_plan_and_preflight(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    sf = session_factory
    alice = await _member(sf, 8301, "alice")
    agent, conn_id = await _agent_with_services(client, sf, alice, allow=["deploy"])
    await agent.post(
        "/agent/v1/events",
        {"states": [state(image="web:1", image_id="sha256:" + "a" * 64)], "events": []},
    )
    conn = await _conn(sf, conn_id)
    target = AgentTarget(_settings(**REGISTRY), None, sf, conn)  # type: ignore[arg-type]
    plan = await target.plan(_spec(conn, uuid.uuid4()))
    ns = registry.namespace(conn.workspace_id)
    assert plan.target == "agent" and plan.image == f"api.gc.test/{ns}/flask-app:abc123def456"
    assert [(s.kind, s.action) for s in plan.steps] == [("image", "push"), ("container", "update")]
    assert plan.previous_revision == "sha256:" + "a" * 64
    checks = await target.preflight(plan)
    assert all(c.status == "pass" for c in checks), checks

    other = await target.plan(_spec(conn, uuid.uuid4(), service="nginx"))
    refused = [c for c in await target.preflight(other) if c.status == "fail"]
    assert refused and "does not allow deploy" in refused[0].detail


async def test_apply_uploads_then_the_agent_fetches_its_secrets_once(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    sf = session_factory
    alice = await _member(sf, 8302, "alice")
    agent, conn_id = await _agent_with_services(client, sf, alice, allow=["deploy"])
    await agent.post("/agent/v1/events", {"states": [state()], "events": []})
    conn = await _conn(sf, conn_id)
    settings = _settings(**REGISTRY)
    target = AgentTarget(settings, None, sf, conn)  # type: ignore[arg-type]
    async with sf() as session:
        ws = await session.get(Workspace, conn.workspace_id)
        assert ws is not None
    run, _, token = await approved_action(sf, ws, action="target_apply", plan_hash="x")
    spec = _spec(conn, run.id)
    plan = await target.plan(spec)
    run, _, token = await approved_action(
        sf, ws, action="target_apply", plan_hash=plan.plan_hash(), run=run
    )
    sandbox = ScriptedSandbox()
    fetched: dict[str, Any] = {}

    async def the_agent() -> None:
        for _ in range(100):
            polled = (await agent.post("/agent/v1/commands/poll", {})).json()["commands"]
            if polled:
                break
            await asyncio.sleep(0.05)
        (command,) = polled
        body = json.loads(command["payload"])
        assert body["action"] == "deploy" and body["params"]["image"] == plan.image
        assert "postgres" not in command["payload"]  # secrets are not in the payload
        got = await agent.post(f"/agent/v1/commands/{command['id']}/secrets", {})
        fetched.update(got.json())
        again = await agent.post(f"/agent/v1/commands/{command['id']}/secrets", {})
        fetched["second_status"] = again.status_code
        await agent.post(
            f"/agent/v1/commands/{command['id']}/result", {"ok": True, "message": "recreated"}
        )

    deployment, _ = await asyncio.gather(target.apply(plan, spec, token, sandbox), the_agent())  # type: ignore[arg-type]
    ns = registry.namespace(conn.workspace_id)
    assert sandbox.pushed == [f"http://registry:5000/{ns}/flask-app:abc123def456"]
    assert deployment.location == f"web-1/{FLASK}" and deployment.revision == plan.image
    login = fetched["registry"]
    assert login["server"] == "api.gc.test" and login["username"] == registry.USERNAME
    assert registry.token_namespace(settings, login["password"]) == ns
    assert {e["name"]: e["value"] for e in fetched["env"]} == {
        "DATABASE_URL": "postgres://db/app",
        "LOG_LEVEL": "info",
    }
    assert fetched["second_status"] == 410
    async with sf() as session:
        command = await session.scalar(select(AgentCommand))
        assert command is not None and command.secret_encrypted is None


async def test_deploys_need_a_service_that_allows_them(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    sf = session_factory
    alice = await _member(sf, 8303, "alice")
    _, conn_id = await _agent_with_services(client, sf, alice, allow=["restart"])
    async with sf() as session:
        gh = Connection(
            workspace_id=alice.workspace_id, kind="github", name="gh", status="connected", config={}
        )
        session.add(gh)
        await session.commit()
    body = {
        "workspace_id": str(alice.workspace_id),
        "repo": {"connection_id": str(gh.id), "repo": "acme/flask", "ref": "main"},
        "target_connection_id": str(conn_id),
        "target_service": FLASK,
    }
    r = await client.post("/deploys", json=body, headers=alice.headers)
    assert r.status_code == 409 and "allows deploy" in r.json()["detail"]


async def test_an_agent_can_use_its_own_registry(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    sf = session_factory
    alice = await _member(sf, 8304, "alice")
    _, conn_id = await _agent_with_services(client, sf, alice, allow=["deploy"])
    r = await client.put(
        f"/connections/{conn_id}/agent/registry",
        json={"registry": "ghcr.io/acme", "username": "bot", "token": "ghp_secret"},
        headers=alice.headers,
    )
    assert r.status_code == 200, r.text
    agent = r.json()["agent"]
    assert agent["registry"] == "ghcr.io/acme" and agent["registry_token_saved"]
    assert "ghp_secret" not in r.text
    conn = await _conn(sf, conn_id)
    target = AgentTarget(_settings(), None, sf, conn)  # type: ignore[arg-type]
    plan = await target.plan(_spec(conn, uuid.uuid4()))
    assert plan.image == "ghcr.io/acme/flask-app:abc123def456"
    login = await target._pull_login()
    assert login == {"server": "ghcr.io", "username": "bot", "password": "ghp_secret"}

    back = await client.delete(f"/connections/{conn_id}/agent/registry", headers=alice.headers)
    assert back.json()["agent"]["registry"] == "groundcrew"
