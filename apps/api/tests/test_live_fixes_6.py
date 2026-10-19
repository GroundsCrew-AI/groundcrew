"""Final broken-env run (2026-10-19). Incident 1, "checkout-worker: 5 error lines", ended "No
fix Medic can apply" with nothing after the colon. That happened even though the diagnosis
was high confidence, named the change that started it (checkout/database-url version cd5bcca5,
as a bare version id), memory found a 0.95 match whose fixes resolved it, and the model
proposed the secret rollback. Every signal was an error spike with `down: false`, and
emergency fixes were only offered when production was down. The deployment-failed incident
opened 6 minutes later took it over and resolved it.

1. A degraded service now gets the emergency fix, still behind approval, when the diagnosis is
   high confidence, names a concrete secret, config, or deploy change, and either the fix
   directly reverts that change or a remembered incident (at or above the threshold) was
   resolved by its fixes. The fix carries `degraded` for the card ("service degraded, not
   down").
2. The taken-over incident shows as Merged in the default list, linked to the one that fixed it.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.actions.fake import FakeEmergencyActions, FakeSecret, FakeService
from app.agents.medic import fixes as fx
from app.agents.medic.events import ChangeMarker, MemoryHit, Signal
from app.agents.medic.jobs import investigate_incident_job
from app.agents.medic.schemas import RootCause
from app.db.models import Connection, Edge, Incident, Resource, Run, RunStatus, Workspace
from app.db.session import SessionFactory
from app.incidents.store import set_status
from app.llm.fake import FakeLLM, reply
from tests.test_api import Member, _member
from tests.test_incidents_api import client as api_client
from tests.test_live_fixes_4 import _open
from tests.test_medic import (
    SECRET_ARN,
    TD,
    Harness,
    _now,
    _state,
    demo_graph,
    eid,
    env_evidence,
    settings,
)
from tests.test_medic import use_deps as medic_deps

pytestmark = pytest.mark.anyio

use_deps = medic_deps
client = api_client

BAD = "cd5bcca5-2e1b-4c0f-90c6-05df3e893b85"


@pytest.fixture
async def gina(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 6601, "gina-live6")


def _rc(ev: Any, *, confidence: str = "high", ref: str = BAD, kind: str = "secret_change") -> str:
    """The run's diagnosis: the triggering change names only the secret's version id."""
    secret_line = eid(ev, "secrets:checkout/database-url", "last changed")
    return json.dumps(
        {
            "summary": "A change to checkout/database-url made the database unreachable.",
            "root_cause": "AWSCURRENT of checkout/database-url is a version with a bad URL.",
            "claims": [{"text": "Secret changed in the window.", "evidence_ids": [secret_line]}],
            "triggering_change": {
                "kind": kind,
                "ref": ref,
                "description": "checkout/database-url secret value changed",
                "evidence_ids": [secret_line],
            },
            "affected_resources": ["checkout-api", "checkout-worker"],
            "confidence": confidence,
            "fix_options": [
                {"kind": "secret_rollback_version", "description": "Roll back the secret."}
            ],
        }
    )


def _actions(sf: SessionFactory, current: str = BAD) -> FakeEmergencyActions:
    return FakeEmergencyActions(
        sf,
        services={
            "checkout/checkout-api": FakeService(
                revisions=[f"{TD}/checkout-api:29", f"{TD}/checkout-api:30"]
            ),
            "checkout/checkout-worker": FakeService(
                revisions=[f"{TD}/checkout-worker:29", f"{TD}/checkout-worker:30"]
            ),
        },
        secrets={
            SECRET_ARN: FakeSecret(versions={"v-good": ["AWSPREVIOUS"], current: ["AWSCURRENT"]})
        },
    )


def _evidence(ids: dict[str, uuid.UUID]) -> Any:
    ev = env_evidence(ids, _now())
    ev.changes = [
        ChangeMarker(
            kind="secret_change",
            ref=f"{SECRET_ARN}:{BAD}",
            at=_now() - timedelta(minutes=12),
            label="checkout/database-url changed",
            resource_id=ids["secret"],
        ),
        ChangeMarker(
            kind="deploy",
            ref=f"{TD}/checkout-api:30",
            at=_now() - timedelta(minutes=11),
            label="checkout-api deployment of checkout-api:30",
            resource_id=ids["api"],
        ),
    ]
    return ev


# --- 1. a degraded service gets the emergency fix ---------------------------------------------


async def test_a_degraded_error_spike_gets_the_secret_revert_marked_degraded(
    session_factory: SessionFactory, workspace: Workspace, use_deps: Any
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    ev = _evidence(ids)
    use_deps(Harness(sf, ev, FakeLLM(reply(_rc(ev)), settings=settings()), actions=_actions(sf)))
    # The worker's spike as in the run: errors up, not every poll failing, so not down.
    incident_id, run_id, _ = await _open(sf, workspace.id, ids["worker"], "log_error_spike", "5")
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})
    incident, state, _ = await _state(sf, incident_id)
    assert incident.status == "waiting_approval"
    assert [f.fix.action for f in state.fixes] == [
        "secret_rollback_version",
        "force_redeploy",
        "force_redeploy",
    ]
    reason = "It reverts the change that started the errors."
    assert {f.fix.degraded for f in state.fixes} == {reason}
    # Still approval-gated: the rollback waits for its approval (the restarts follow it).
    assert state.fixes[0].status == "waiting_approval" and state.fixes[0].approval_id


async def test_a_degraded_spike_with_medium_confidence_says_why_there_is_no_fix(
    session_factory: SessionFactory, workspace: Workspace, use_deps: Any
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    ev = _evidence(ids)
    llm = FakeLLM(reply(_rc(ev, confidence="medium")), settings=settings())
    use_deps(Harness(sf, ev, llm, actions=_actions(sf)))
    incident_id, run_id, _ = await _open(sf, workspace.id, ids["worker"], "log_error_spike", "5")
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})
    _, state, _ = await _state(sf, incident_id)
    assert state.fixes == []
    assert state.notes == [
        "The service is degraded, not down, and the diagnosis is medium confidence, not high, "
        "so no emergency action is offered."
    ]


# Unit cases on fx.emergency: the gate's other branches.


async def _graph(sf: SessionFactory, ws: uuid.UUID, affected: list[uuid.UUID]) -> fx.Graph:
    async with sf() as session:
        resources = {
            r.id: r
            for r in await session.scalars(select(Resource).where(Resource.workspace_id == ws))
        }
        edges = list(await session.scalars(select(Edge).where(Edge.workspace_id == ws)))
        conns = {
            c.id: c
            for c in await session.scalars(select(Connection).where(Connection.workspace_id == ws))
        }
    return fx.Graph(resources, edges, conns, affected)


def _spike(rid: uuid.UUID) -> Signal:
    return Signal(
        rule="log_error_spike", resource_id=rid, resource_name="svc", detail="5", at=_now()
    )


def _root(**change: Any) -> RootCause:
    return RootCause(
        summary="s",
        root_cause="r",
        triggering_change=change or None,
        confidence="high",
    )


MEMORY = MemoryHit(
    incident_id=uuid.uuid4(),
    title="checkout-worker: 6 error lines",
    similarity=0.95,
    root_cause="checkout/database-url changed",
    fix_summary="Roll back checkout/database-url to its previous version",
    resolved_at=datetime(2026, 10, 16, 5, 55, tzinfo=UTC),
)


async def test_a_rollback_that_is_not_the_revert_needs_a_remembered_fix(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    """Someone already rotated the secret again: rolling back no longer undoes cd5bcca5."""
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    graph = await _graph(sf, workspace.id, [ids["worker"]])
    changes = _evidence(ids).changes
    actions = _actions(sf, current="v-newer")
    root = _root(kind="secret_change", ref=BAD)
    signals = [_spike(ids["worker"])]

    alone = await fx.emergency(root, signals, graph, changes, actions)
    assert alone.records == [] and "would not revert" in alone.notes[0]

    remembered = await fx.emergency(root, signals, graph, changes, actions, memory=MEMORY)
    assert remembered.records[0].fix.action == "secret_rollback_version"
    assert remembered.records[0].fix.degraded == (
        "The same fix resolved checkout-worker: 6 error lines (95% similar)."
    )
    # A remembered incident without applied fixes is not enough.
    unfixed = MEMORY.model_copy(update={"fix_summary": None})
    out = await fx.emergency(root, signals, graph, changes, actions, memory=unfixed)
    assert out.records == []


async def test_a_degraded_service_after_a_deploy_gets_that_deploy_rolled_back(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    graph = await _graph(sf, workspace.id, [ids["api"], ids["worker"]])
    changes = _evidence(ids).changes
    root = _root(kind="deploy", ref=f"{TD}/checkout-api:30")
    out = await fx.emergency(
        root, [_spike(ids["api"]), _spike(ids["worker"])], graph, changes, _actions(sf)
    )
    # Only checkout-api: its deploy started it; the worker was not deployed.
    (rec,) = out.records
    assert rec.fix.action == "ecs_rollback_service" and rec.fix.plan is not None
    assert rec.fix.plan.params["service"] == "checkout-api"
    assert rec.fix.degraded == "It reverts the change that started the errors."


async def test_a_commit_or_no_change_never_gets_a_degraded_emergency_fix(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    graph = await _graph(sf, workspace.id, [ids["worker"]])
    changes = _evidence(ids).changes
    signals = [_spike(ids["worker"])]
    for root in (_root(kind="commit", ref="abc1234"), _root()):
        out = await fx.emergency(root, signals, graph, changes, _actions(sf), memory=MEMORY)
        assert out.records == []


async def test_a_down_service_is_unchanged_and_not_marked_degraded(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    graph = await _graph(sf, workspace.id, [ids["worker"]])
    down = _spike(ids["worker"]).model_copy(update={"down": True})
    root = _root(kind="secret_change", ref=BAD).model_copy(update={"confidence": "low"})
    out = await fx.emergency(root, [down], graph, _evidence(ids).changes, _actions(sf))
    assert out.records[0].fix.action == "secret_rollback_version"
    assert {r.fix.degraded for r in out.records} == {None}


# --- 2. the taken-over incident in the list --------------------------------------------------


async def test_a_taken_over_incident_is_listed_as_merged_with_the_incident_that_fixed_it(
    session_factory: SessionFactory, client: httpx.AsyncClient, gina: Member
) -> None:
    sf = session_factory
    ws = gina.workspace_id
    ids = await demo_graph(sf, ws)
    worker_id, run_id, _ = await _open(sf, ws, ids["worker"], "log_error_spike", "5 lines")
    async with sf() as session:
        worker = await session.get_one(Incident, worker_id)
        run = await session.get_one(Run, run_id)
        await set_status(session, worker, run, "failed", reason="No fix")
        worker.window_end = datetime.now(UTC)
        run.status = RunStatus.succeeded
        await session.commit()
    api_id, _, deduped = await _open(
        sf, ws, ids["api"], "ecs_deployment_failed", "deployment failed", down=True
    )
    assert not deduped

    listed = await client.get("/incidents", params={"workspace_id": str(ws)}, headers=gina.headers)
    rows = {i["id"]: i for i in listed.json()["items"]}
    assert set(rows) == {str(api_id), str(worker_id)}
    merged = rows[str(worker_id)]
    assert merged["status"] == "merged" and merged["merged_into"] == str(api_id)
    async with sf() as session:
        title = (await session.get_one(Incident, api_id)).title
    assert merged["merged_into_title"] == title
    assert rows[str(api_id)]["merged_into"] is None
    detail = (await client.get(f"/incidents/{worker_id}", headers=gina.headers)).json()
    assert detail["merged_into_title"] == title
