"""Agent signals and fixes (A2 + A3): service states and events in, detector rules, incidents on
agent services, signed commands out (long poll, result), the agent restart action, and Medic
restarting a stopped service end to end with a simulated agent."""

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import SecretStr
from sqlalchemy import select

from app.actions.agent import AgentEmergencyActions
from app.actions.base import EmergencyActionError, EmergencyActionRouter
from app.agents.medic.jobs import investigate_incident_job, verify_incident_job
from app.config import Settings, get_settings
from app.db.models import Agent, AgentCommand, AgentEvent, Incident, Resource, Workspace
from app.db.session import SessionFactory, get_session_factory
from app.discovery.engine import DiscoveryEngine
from app.llm.fake import FakeLLM, reply
from app.main import app
from app.remote_agents import commands
from app.remote_agents.routes import POLL_SECONDS
from app.security.ratelimit import limiter
from app.telemetry.collect import Evidence
from app.telemetry.evidence import line
from tests.medic_fakes import approve_medic_action, medic_run
from tests.test_api import Member, _member, _settings
from tests.test_aws_compute_incidents import _approve_all
from tests.test_medic import Harness, _state, eid
from tests.test_medic import settings as medic_settings
from tests.test_medic import use_deps as medic_deps
from tests.test_remote_agents import INVENTORY, FakeAgent, _add

pytestmark = pytest.mark.anyio
use_deps = medic_deps

FLASK = "legacy-flask-app"


@pytest.fixture
async def client(session_factory: SessionFactory) -> AsyncIterator[httpx.AsyncClient]:
    limiter.reset()
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = lambda: _settings(incident_watch_enabled=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def state(name: str = FLASK, **kw: Any) -> dict[str, Any]:
    return {"name": name, "found": True, "state": "running", "restarts": 0, **kw}


async def _agent_with_services(
    client: httpx.AsyncClient,
    sf: SessionFactory,
    owner: Member,
    allow: list[str] | None = None,
) -> tuple[FakeAgent, uuid.UUID]:
    """An enrolled agent whose inventory reached the Map (`allow` for the flask service)."""
    added = await _add(client, owner)
    agent = FakeAgent(client)
    assert (await agent.enroll(added["token"])).status_code == 200
    conn_id = uuid.UUID(added["connection"]["id"])
    inventory = INVENTORY
    if allow is not None:
        services = [dict(INVENTORY["services"][0], allow=allow), *INVENTORY["services"][1:]]
        inventory = {**INVENTORY, "services": services}
    r = await agent.post("/agent/v1/inventory", inventory)
    await DiscoveryEngine(sf).run(uuid.UUID(r.json()["discovery_run_id"]), connection_id=conn_id)
    return agent, conn_id


async def _incidents(sf: SessionFactory) -> list[Incident]:
    async with sf() as session:
        return list(await session.scalars(select(Incident).order_by(Incident.created_at)))


def test_command_signatures_match_the_go_agent_vector() -> None:
    """The same vector is checked by apps/agent/internal/commands/commands_test.go."""
    from app.db.models import Agent as AgentRow

    s = Settings(
        _env_file=None, approval_token_key=SecretStr("test-approval-token-key-0123456789abcdef")
    )
    assert commands.command_public_key(s) == "HdkFbZVKrBHI6ZrCwo0gvC3OW/ZX29fv4XvxlD2xBtk="
    row = AgentRow(
        id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        workspace_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
    )
    c = commands.issue(
        row,
        action="restart",
        service="web",
        plan_hash="ab" * 32,
        run_id=None,
        settings=s,
        now=datetime(2026, 10, 22, 10, 0, tzinfo=UTC),
    )
    body = json.loads(c.payload)
    assert body["agent_id"] == str(row.id) and body["expires_at"] == "2026-10-22T10:05:00+00:00"
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(commands.command_public_key(s)))
    key.verify(base64.b64decode(c.signature), c.payload.encode())  # raises if it does not match


async def test_a_stopped_service_opens_one_incident_and_events_are_kept(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    alice = await _member(session_factory, 8101, "alice")
    agent, _ = await _agent_with_services(client, session_factory, alice)
    ok = await agent.post("/agent/v1/events", {"states": [state()], "events": []})
    assert ok.status_code == 200, ok.text
    assert await _incidents(session_factory) == []

    now = datetime.now(UTC).isoformat()
    down = {
        "states": [state(state="exited", exit_code=137, oom_killed=True, restarts=1)],
        "events": [
            {
                "service": FLASK,
                "at": now,
                "kind": "state",
                "message": "exited (exit code 137, OOM killed)",
            },
            {
                "service": FLASK,
                "at": now,
                "kind": "log",
                "message": "ERROR password=hunter2 rejected",
            },
        ],
    }
    assert (await agent.post("/agent/v1/events", down)).status_code == 200
    (incident,) = await _incidents(session_factory)
    assert incident.title.startswith(f"{FLASK}: {FLASK} exited (exit code 137, OOM killed)")
    assert incident.signals[0]["rule"] == "agent_service_down" and incident.signals[0]["down"]
    assert incident.severity == "critical"

    # Still down: no second incident, and the store is not asked again.
    assert (
        await agent.post("/agent/v1/events", {"states": down["states"], "events": []})
    ).status_code == 200
    assert len(await _incidents(session_factory)) == 1
    async with session_factory() as session:
        stored = list(await session.scalars(select(AgentEvent).order_by(AgentEvent.id)))
        row = await session.scalar(select(Agent))
    assert [e.kind for e in stored] == ["state", "log"]
    assert "hunter2" not in stored[1].message  # redacted again on arrival
    assert row is not None and row.firing == {FLASK: ["agent_service_down"]}


async def test_failed_health_checks_and_error_spikes_fire(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    alice = await _member(session_factory, 8102, "alice")
    agent, _ = await _agent_with_services(client, session_factory, alice)
    two = state(health_failures=2, health_detail="HTTP 502")
    await agent.post("/agent/v1/events", {"states": [two], "events": []})
    assert await _incidents(session_factory) == []
    three = state(health_failures=3, health_detail="HTTP 502")
    now = datetime.now(UTC).isoformat()
    errors = [
        {"service": "nginx", "at": now, "kind": "log", "message": f"[error] upstream timed out {i}"}
        for i in range(10)
    ]
    await agent.post(
        "/agent/v1/events", {"states": [three, state("nginx", state="active")], "events": errors}
    )
    rules = sorted(s["rule"] for i in await _incidents(session_factory) for s in i.signals)
    assert rules == ["health_check", "log_error_spike"]


async def test_reports_are_ignored_for_incidents_when_watching_is_off(
    session_factory: SessionFactory,
) -> None:
    limiter.reset()
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = lambda: _settings(incident_watch_enabled=False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            alice = await _member(session_factory, 8103, "alice")
            agent, _ = await _agent_with_services(c, session_factory, alice)
            r = await agent.post(
                "/agent/v1/events", {"states": [state(state="exited")], "events": []}
            )
            assert r.status_code == 200
    finally:
        app.dependency_overrides.clear()
    assert await _incidents(session_factory) == []


async def test_commands_are_handed_out_once_and_their_result_recorded(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    alice = await _member(session_factory, 8104, "alice")
    agent, _ = await _agent_with_services(client, session_factory, alice)
    async with session_factory() as session:
        row = await session.scalar(select(Agent))
        assert row is not None
        command = commands.issue(
            row,
            action="restart",
            service=FLASK,
            plan_hash="0" * 64,
            run_id=None,
            settings=get_settings(),
        )
        stale = commands.issue(
            row,
            action="restart",
            service=FLASK,
            plan_hash="1" * 64,
            run_id=None,
            settings=get_settings(),
            now=datetime.now(UTC) - timedelta(minutes=10),
        )
        session.add_all([command, stale])
        await session.commit()
    beat = await agent.post("/agent/v1/heartbeat", {"version": "0.2.0", "uptime_seconds": 5})
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(beat.json()["command_key"]))

    polled = await agent.post("/agent/v1/commands/poll", {})
    (got,) = polled.json()["commands"]
    assert got["id"] == str(command.id)
    key.verify(base64.b64decode(got["signature"]), got["payload"].encode())

    done = await agent.post(
        f"/agent/v1/commands/{command.id}/result", {"ok": True, "message": "restarted"}
    )
    assert done.status_code == 200
    async with session_factory() as session:
        rows = {c.id: c for c in await session.scalars(select(AgentCommand))}
        logged = await session.scalar(select(AgentEvent).where(AgentEvent.kind == "command"))
    assert rows[command.id].status == "succeeded" and rows[stale.id].status == "expired"
    assert logged is not None and logged.message == "restart done: restarted"

    other = await _member(session_factory, 8105, "bob")
    intruder, _ = await _agent_with_services(client, session_factory, other)
    r = await intruder.post(
        f"/agent/v1/commands/{command.id}/result", {"ok": False, "message": "x"}
    )
    assert r.status_code == 404


async def test_an_empty_poll_waits_then_returns(
    client: httpx.AsyncClient, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.remote_agents import routes

    monkeypatch.setattr(routes, "POLL_SECONDS", 1)
    monkeypatch.setattr(routes, "POLL_CHECK_SECONDS", 0.2)
    assert POLL_SECONDS == 25
    alice = await _member(session_factory, 8106, "alice")
    agent, _ = await _agent_with_services(client, session_factory, alice)
    assert (await agent.post("/agent/v1/commands/poll", {})).json() == {"commands": []}


async def _fake_agent_runs_commands(sf: SessionFactory, *, ok: bool = True) -> str:
    """What the Go agent does: pick the command up, restart, report, then send a fresh state."""
    for _ in range(100):
        await asyncio.sleep(0.05)
        async with sf() as session:
            c = await session.scalar(select(AgentCommand).where(AgentCommand.status == "queued"))
            if c is None:
                continue
            c.status, c.sent_at = "sent", datetime.now(UTC)
            c.status = "succeeded" if ok else "failed"
            c.result = "restarted; it is running" if ok else "Docker answered 500"
            c.finished_at = datetime.now(UTC)
            row = await session.get(Agent, c.agent_id)
            assert row is not None
            if ok:
                after = state(c.service, restarts=2)
                image = json.loads(c.payload)["params"].get("image")
                if image:
                    after |= {"image": "web:1", "image_id": image}
                row.states = {**row.states, c.service: after}
                row.states_at = datetime.now(UTC) + timedelta(seconds=1)
            await session.commit()
            return c.payload
    raise AssertionError("no command was queued")


async def test_restart_plans_need_an_online_agent_and_the_services_allow(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    alice = await _member(session_factory, 8107, "alice")
    agent, conn_id = await _agent_with_services(client, session_factory, alice)
    await agent.post(
        "/agent/v1/events",
        {
            "states": [state(state="exited", exit_code=0), state("nginx", state="failed")],
            "events": [],
        },
    )
    actions = AgentEmergencyActions(session_factory, _settings())
    plan = await actions.plan_agent_restart_service(conn_id, FLASK)
    assert plan.current == f"{FLASK} exited (exit code 0)" and plan.target == f"web-1/{FLASK}"
    with pytest.raises(EmergencyActionError, match="does not allow restart"):
        await actions.plan_agent_restart_service(conn_id, "nginx")
    with pytest.raises(EmergencyActionError, match="does not report"):
        await actions.plan_agent_restart_service(conn_id, "redis")
    async with session_factory() as session:
        row = await session.scalar(select(Agent))
        assert row is not None
        row.last_seen_at = datetime.now(UTC) - timedelta(minutes=10)
        await session.commit()
    with pytest.raises(EmergencyActionError, match="offline"):
        await actions.plan_agent_restart_service(conn_id, FLASK)


async def test_a_restart_runs_through_a_signed_command_and_not_after_the_state_moved(
    client: httpx.AsyncClient, session_factory: SessionFactory, workspace: Workspace
) -> None:
    sf = session_factory
    async with sf() as session:
        ws = await session.get(Workspace, workspace.id)
        assert ws is not None
    alice = await _member(sf, 8108, "alice")
    agent, conn_id = await _agent_with_services(client, sf, alice)
    await agent.post(
        "/agent/v1/events", {"states": [state(state="exited", exit_code=0)], "events": []}
    )
    actions = AgentEmergencyActions(sf, _settings())
    plan = await actions.plan_agent_restart_service(conn_id, FLASK)
    run = await medic_run(sf, workspace)
    token = await approve_medic_action(
        sf, workspace, run, action=plan.action, plan_hash=plan.plan_hash()
    )
    result, payload = await asyncio.gather(
        actions.execute(plan, token, run_id=run.id), _fake_agent_runs_commands(sf)
    )
    assert result.ok and result.summary == f"Restarted {FLASK} on web-1."
    assert json.loads(payload)["plan_hash"] == plan.plan_hash()

    # A second approval of the old plan: the service runs now, so nothing is sent.
    token = await approve_medic_action(
        sf, workspace, run, action=plan.action, plan_hash=plan.plan_hash()
    )
    moved = await actions.execute(plan, token, run_id=run.id)
    assert not moved.ok and "changed since the plan" in moved.summary


async def test_medic_restarts_a_stopped_service_end_to_end(
    client: httpx.AsyncClient, session_factory: SessionFactory, workspace: Workspace, use_deps: Any
) -> None:
    sf = session_factory
    owner = await _member(sf, 8109, "owner")
    agent, conn_id = await _agent_with_services(client, sf, owner)
    await agent.post("/agent/v1/events", {"states": [state()], "events": []})
    stopped = {
        "states": [state(state="exited", exit_code=0)],
        "events": [
            {
                "service": FLASK,
                "at": datetime.now(UTC).isoformat(),
                "kind": "state",
                "message": "exited (exit code 0)",
            }
        ],
    }
    await agent.post("/agent/v1/events", stopped)
    (incident,) = await _incidents(sf)
    async with sf() as session:
        svc = await session.scalar(select(Resource).where(Resource.name == FLASK))
        ws = await session.get(Workspace, incident.workspace_id)
        assert svc is not None and ws is not None
    ev = Evidence()
    key = f"agent:{FLASK}"
    ev.sources[key] = [line(datetime.now(UTC), "agent", FLASK, "[state] exited (exit code 0)")]
    ev.truncated[key] = False
    ev.source_resources[key] = svc.id
    root = {
        "summary": f"{FLASK} stopped on web-1.",
        "root_cause": "The container exited with code 0: it was stopped.",
        "claims": [{"text": "It exited.", "evidence_ids": [eid(ev, key, "exited")]}],
        "triggering_change": None,
        "affected_resources": [FLASK],
        "confidence": "high",
        "fix_options": [{"kind": "agent_restart_service", "description": "Restart it."}],
    }
    router = EmergencyActionRouter(None, None, AgentEmergencyActions(sf, _settings()))
    use_deps(
        Harness(
            sf,
            ev,
            FakeLLM(reply(json.dumps(root)), settings=medic_settings()),
            actions=cast(Any, router),
        )
    )
    run_id = (await _state(sf, incident.id))[2].id
    await investigate_incident_job(sf, {"incident_id": str(incident.id), "run_id": str(run_id)})
    inc, st, _ = await _state(sf, incident.id)
    assert inc.status == "waiting_approval", st.error or st.notes
    (fix,) = st.fixes
    assert (
        fix.fix.action == "agent_restart_service" and fix.fix.title == f"Restart {FLASK} on web-1"
    )
    await asyncio.gather(_approve_all(sf, ws, incident.id, run_id), _fake_agent_runs_commands(sf))
    payload = {"incident_id": str(incident.id), "run_id": str(run_id)}
    # The agent reports right after a command and every 10 seconds: each round reads a fresh one.
    for _ in range(2):
        await asyncio.sleep(1.1)  # report times are compared to the second the fix applied
        await agent.post("/agent/v1/events", {"states": [state(restarts=2)], "events": []})
        await verify_incident_job(sf, payload)
    inc, st, _ = await _state(sf, incident.id)
    assert inc.status == "resolved", st.verify
    assert (
        st.verify is not None
        and st.verify.checks[0].detail == f"{FLASK} running, health checks passing"
    )
    assert conn_id is not None


BAD = {"image": "web:2", "image_id": "sha256:" + "b" * 64}
GOOD = {"previous_image": "web:1", "previous_image_id": "sha256:" + "a" * 64}


async def test_a_rollback_goes_back_to_the_previous_image_through_a_signed_command(
    client: httpx.AsyncClient, session_factory: SessionFactory, workspace: Workspace
) -> None:
    sf = session_factory
    alice = await _member(sf, 8110, "alice")
    agent, conn_id = await _agent_with_services(client, sf, alice, allow=["restart", "rollback"])
    await agent.post(
        "/agent/v1/events",
        {"states": [state(state="exited", exit_code=3, **BAD, **GOOD)], "events": []},
    )
    actions = AgentEmergencyActions(sf, _settings())
    plan = await actions.plan_agent_rollback_service(conn_id, FLASK)
    assert plan.current == "web:2@bbbbbbbbbbbb" and plan.desired == "web:1@aaaaaaaaaaaa"
    assert plan.params["image"] == GOOD["previous_image_id"]
    run = await medic_run(sf, workspace)
    token = await approve_medic_action(
        sf, workspace, run, action=plan.action, plan_hash=plan.plan_hash()
    )
    result, payload = await asyncio.gather(
        actions.execute(plan, token, run_id=run.id), _fake_agent_runs_commands(sf)
    )
    assert result.ok and result.summary == f"Rolled {FLASK} on web-1 back to web:1@aaaaaaaaaaaa."
    sent = json.loads(payload)
    assert sent["action"] == "rollback" and sent["params"] == {
        "image": GOOD["previous_image_id"],
        "ref": "web:1",
    }

    # The image changed again since the plan: nothing is sent.
    token = await approve_medic_action(
        sf, workspace, run, action=plan.action, plan_hash=plan.plan_hash()
    )
    moved = await actions.execute(plan, token, run_id=run.id)
    assert not moved.ok and "changed since the plan" in moved.summary


async def test_a_rollback_needs_its_allow_and_an_earlier_image(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    alice = await _member(session_factory, 8111, "alice")
    agent, conn_id = await _agent_with_services(client, session_factory, alice)
    actions = AgentEmergencyActions(session_factory, _settings())
    await agent.post(
        "/agent/v1/events", {"states": [state(state="exited", **BAD, **GOOD)], "events": []}
    )
    with pytest.raises(EmergencyActionError, match="does not allow rollback"):
        await actions.plan_agent_rollback_service(conn_id, FLASK)

    bob = await _member(session_factory, 8112, "bob")
    agent, conn_id = await _agent_with_services(client, session_factory, bob, allow=["rollback"])
    await agent.post("/agent/v1/events", {"states": [state(state="exited", **BAD)], "events": []})
    with pytest.raises(EmergencyActionError, match="no earlier image"):
        await actions.plan_agent_rollback_service(conn_id, FLASK)


async def test_medic_rolls_back_a_service_that_broke_after_its_image_changed(
    client: httpx.AsyncClient, session_factory: SessionFactory, use_deps: Any
) -> None:
    sf = session_factory
    owner = await _member(sf, 8113, "owner")
    agent, _ = await _agent_with_services(client, sf, owner, allow=["restart", "rollback"])
    await agent.post(
        "/agent/v1/events",
        {"states": [state(image="web:1", image_id=GOOD["previous_image_id"])], "events": []},
    )
    now = datetime.now(UTC).isoformat()
    broke = {
        "states": [state(state="exited", exit_code=3, **BAD, **GOOD)],
        "events": [
            {
                "service": FLASK,
                "at": now,
                "kind": "deploy",
                "message": "image changed from web:1@aaaaaaaaaaaa to web:2@bbbbbbbbbbbb",
            },
            {"service": FLASK, "at": now, "kind": "state", "message": "exited (exit code 3)"},
        ],
    }
    await agent.post("/agent/v1/events", broke)
    (incident,) = await _incidents(sf)
    async with sf() as session:
        svc = await session.scalar(select(Resource).where(Resource.name == FLASK))
        ws = await session.get(Workspace, incident.workspace_id)
        assert svc is not None and ws is not None
    from app.agents.medic.events import ChangeMarker

    ev = Evidence()
    key = f"agent:{FLASK}"
    ev.sources[key] = [line(datetime.now(UTC), "agent", FLASK, "[state] exited (exit code 3)")]
    ev.truncated[key] = False
    ev.source_resources[key] = svc.id
    ev.changes = [
        ChangeMarker(
            kind="deploy",
            ref=f"web-1/{FLASK}@web:2@bbbbbbbbbbbb",
            at=datetime.now(UTC) - timedelta(seconds=30),
            resource_id=svc.id,
            label="image changed",
        )
    ]
    root = {
        "summary": f"{FLASK} exits since its image changed to web:2.",
        "root_cause": "The new image exits with code 3 at start.",
        "claims": [{"text": "It exits.", "evidence_ids": [eid(ev, key, "exited")]}],
        "triggering_change": {"kind": "deploy", "ref": f"web-1/{FLASK}@web:2@bbbbbbbbbbbb"},
        "affected_resources": [FLASK],
        "confidence": "high",
        "fix_options": [{"kind": "agent_rollback_service", "description": "Roll back."}],
    }
    router = EmergencyActionRouter(None, None, AgentEmergencyActions(sf, _settings()))
    use_deps(
        Harness(
            sf,
            ev,
            FakeLLM(reply(json.dumps(root)), settings=medic_settings()),
            actions=cast(Any, router),
        )
    )
    run_id = (await _state(sf, incident.id))[2].id
    await investigate_incident_job(sf, {"incident_id": str(incident.id), "run_id": str(run_id)})
    inc, st, _ = await _state(sf, incident.id)
    assert inc.status == "waiting_approval", st.error or st.notes
    (fix,) = st.fixes
    assert fix.fix.action == "agent_rollback_service"
    assert fix.fix.title == f"Roll back {FLASK} on web-1 to web:1@aaaaaaaaaaaa"
    await asyncio.gather(_approve_all(sf, ws, incident.id, run_id), _fake_agent_runs_commands(sf))
    payload = {"incident_id": str(incident.id), "run_id": str(run_id)}
    for _ in range(2):
        await asyncio.sleep(1.1)
        await agent.post(
            "/agent/v1/events",
            {"states": [state(image="web:1", image_id=GOOD["previous_image_id"])], "events": []},
        )
        await verify_incident_job(sf, payload)
    inc, st, _ = await _state(sf, incident.id)
    assert inc.status == "resolved", st.verify
