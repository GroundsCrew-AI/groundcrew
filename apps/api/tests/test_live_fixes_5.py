"""Live broken-env rerun, round 5 (2026-10-14, incident c9c38b96):

2. New signals at 11:03:49 re-diagnosed the incident; it proposed the very same three fixes, but
   their approvals had been rejected first ("superseded by new signals"), so the person approved
   the same fixes twice.
3. "What changed" was mostly "checkout-demo deployment READY": the API_URL Lambda's (tooling)
   Vercel redeploys of the same commit, every few minutes.
4. The strip's "First signal" was the earliest signal of the incident it took over (10:13), not
   the opening signal's onset (10:57:59).
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.actions.fake import FakeEmergencyActions
from app.agents.medic.agent import _same_fixes
from app.agents.medic.events import ChangeMarker, Signal
from app.agents.medic.jobs import investigate_incident_job, rediagnose_incident_job
from app.db.models import Approval, ApprovalStatus, Connection, Incident, Job, Resource, Workspace
from app.db.session import SessionFactory
from app.incidents.store import REDIAGNOSE_JOB, open_incident
from app.llm.fake import FakeLLM, reply
from app.pipeline.resume import resume_run_job
from app.policy.engine import Decision, decide_approval
from app.telemetry.collect import _without_tooling
from app.telemetry.vercel import read_project
from tests.test_api import Member, _member
from tests.test_incidents_api import client as api_client
from tests.test_live_fixes_3 import MIGRATION, SHA, _index_case, _open_api
from tests.test_medic import (
    Harness,
    _now,
    _state,
    demo_graph,
    github_routes,
    settings,
)
from tests.test_medic import use_deps as medic_deps

pytestmark = pytest.mark.anyio

use_deps = medic_deps
client = api_client


@pytest.fixture
async def frank(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 6401, "frank-live5")


# --- 2. a re-diagnosis that proposes the same fixes keeps their approvals ------------------------


def _other_fix() -> str:
    return json.dumps(
        {
            "title": "Recreate orders.customer_email index",
            "body": "A different migration.",
            "rationale": "r",
            "files": [{"path": MIGRATION, "content": "CREATE INDEX ix ON orders (email);\n"}],
        }
    )


async def _new_signal(sf: SessionFactory, ws: uuid.UUID, api: uuid.UUID) -> dict[str, Any]:
    async with sf() as session:
        again = await open_incident(
            session,
            workspace_id=ws,
            resource_ids=[api],
            detected_by="watcher",
            signals=[
                Signal(
                    rule="rds_cpu_latency",
                    resource_id=api,
                    resource_name="checkout-api",
                    detail="/orders/search p50 13860 ms (baseline 13 ms), CPU 56%",
                    at=_now(),
                )
            ],
        )
        job = await session.scalar(select(Job).where(Job.kind == REDIAGNOSE_JOB))
        assert again.deduped and job is not None
        return dict(job.payload)


class ApprovesDuringDiagnosis(FakeLLM):
    """A person approves the waiting fix while Medic diagnoses again (third model call)."""

    approve: tuple[SessionFactory, Workspace, uuid.UUID] | None = None

    async def _create(self, kwargs: dict[str, Any]) -> Any:
        if len(self.calls) == 2 and self.approve is not None:
            sf, ws, approval_id = self.approve
            async with sf() as session:
                await decide_approval(session, approval_id, Decision.approve, ws.owner_id)
        return await super()._create(kwargs)


async def test_an_approval_given_during_the_rediagnosis_of_the_same_fix_is_applied(
    session_factory: SessionFactory, workspace: Workspace, use_deps: Any
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    ev, rc, fix = _index_case(ids, _now(), "First look.")
    _, rc2, _ = _index_case(ids, _now(), "Searches time out: the index is gone.")
    llm = ApprovesDuringDiagnosis(
        reply(rc), reply(fix), reply(rc2), reply(fix), settings=settings(dev_mode_no_ultra=True)
    )
    routes = github_routes(SHA, {"m.py": ("+op.drop_index('ix')", "x\n")})
    h = Harness(sf, ev, llm, actions=FakeEmergencyActions(sf), gh=routes)
    use_deps(h)
    incident_id, run_id = await _open_api(sf, workspace.id, ids["api"])
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})
    _, state, _ = await _state(sf, incident_id)
    first = state.fixes[0].approval_id
    assert first is not None
    llm.approve = (sf, workspace, first)

    await rediagnose_incident_job(sf, await _new_signal(sf, workspace.id, ids["api"]))
    _, state, _ = await _state(sf, incident_id)
    assert state.fixes[0].approval_id == first and state.fixes[0].fix.fix_id == "fix-1"
    async with sf() as session:
        job = await session.scalar(select(Job).where(Job.kind == "resume_run"))
        assert job is not None  # the decision made meanwhile goes on
    await resume_run_job(sf, dict(job.payload))
    _, state, _ = await _state(sf, incident_id)
    assert state.fixes[0].status == "applied" and len(h.prs.opened) == 1


async def test_a_different_fix_closes_the_old_approval_and_asks_again(
    session_factory: SessionFactory, workspace: Workspace, use_deps: Any
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    ev, rc, fix = _index_case(ids, _now(), "First look.")
    _, rc2, _ = _index_case(ids, _now(), "Searches time out: the index is gone.")
    llm = FakeLLM(
        reply(rc),
        reply(fix),
        reply(rc2),
        reply(_other_fix()),
        settings=settings(dev_mode_no_ultra=True),
    )
    routes = github_routes(SHA, {"m.py": ("+op.drop_index('ix')", "x\n")})
    use_deps(Harness(sf, ev, llm, actions=FakeEmergencyActions(sf), gh=routes))
    incident_id, run_id = await _open_api(sf, workspace.id, ids["api"])
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})
    _, state, _ = await _state(sf, incident_id)
    first = state.fixes[0].approval_id

    await rediagnose_incident_job(sf, await _new_signal(sf, workspace.id, ids["api"]))
    incident, state, _ = await _state(sf, incident_id)
    assert state.fixes[0].fix.fix_id == "fix-1-r1" and state.fixes[0].approval_id != first
    async with sf() as session:
        old = await session.get_one(Approval, first)
        assert old.status == ApprovalStatus.rejected
        assert old.decided_via == "Medic: superseded by new signals"
    assert incident.status == "waiting_approval" and state.superseded == []


def test_same_fixes_compares_plans_not_wording() -> None:
    from app.agents.medic.state import FixRecord
    from tests.test_incidents_api import _state as detail_state

    records = detail_state(uuid.uuid4(), uuid.uuid4(), uuid.uuid4()).fixes
    reworded = [
        FixRecord.model_validate(
            {**r.model_dump(), "fix": {**r.fix.model_dump(), "title": f"{r.fix.title}!"}}
        )
        for r in records
    ]
    assert _same_fixes(records, reworded)
    assert not _same_fixes(records, records[:1])
    assert not _same_fixes(records, list(reversed(records)))


# --- 3. what changed: no tooling, redeploys say so ------------------------------------------------


async def test_changes_to_tooling_are_left_out_unless_named(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    async with session_factory() as session:
        conn = Connection(workspace_id=workspace.id, kind="aws", name="a", status="connected")
        session.add(conn)
        await session.flush()
        lam = Resource(
            workspace_id=workspace.id,
            connection_id=conn.id,
            provider="aws",
            type="lambda:function",
            name="checkout-vercel-api-url",
            external_id="arn:lambda",
            tags={"groundcrew:role": "tooling"},
        )
        api = Resource(
            workspace_id=workspace.id,
            connection_id=conn.id,
            provider="aws",
            type="ecs:service",
            name="checkout-api",
            external_id="arn:svc",
        )
        session.add_all([lam, api])
        await session.commit()
    at = datetime(2026, 10, 14, 10, 57, tzinfo=UTC)
    changes = [
        ChangeMarker(kind="deploy", ref="l1", at=at, label="lambda", resource_id=lam.id),
        ChangeMarker(kind="deploy", ref="a1", at=at, label="api", resource_id=api.id),
    ]
    kept = await _without_tooling(session_factory, workspace.id, changes, set())
    assert [c.label for c in kept] == ["api"]
    named = await _without_tooling(session_factory, workspace.id, changes, {lam.id})
    assert [c.label for c in named] == ["lambda", "api"]


class _Vercel:
    def __init__(self, deployments: list[dict[str, Any]]) -> None:
        self.deployments = deployments

    async def production_deployments(self, project_id: str, limit: int) -> list[dict[str, Any]]:
        return self.deployments


async def test_vercel_redeploys_say_so_and_tooling_ones_are_skipped() -> None:
    t0 = datetime(2026, 10, 14, 10, 50, tzinfo=UTC)

    def d(n: int, source: str, tooling: bool = False) -> dict[str, Any]:
        return {
            "uid": f"dpl_{n}",
            "url": f"checkout-demo-{n}.vercel.app",
            "state": "READY",
            "created_at": int((t0 + timedelta(minutes=n)).timestamp() * 1000),
            "commit_sha": "5f638080000",
            "commit_message": "Merge pull request #4",
            "source": source,
            "tooling": tooling,
        }

    out = await read_project(
        _Vercel([d(1, "git"), d(2, "redeploy"), d(3, "redeploy", tooling=True)]),  # type: ignore[arg-type]
        "prj_1",
        "checkout-demo",
        t0,
        t0 + timedelta(hours=1),
    )
    assert [c.label for c in out.changes] == [
        "checkout-demo deployment READY",
        "checkout-demo redeploy READY (same source, commit 5f63808)",
    ]


def test_the_vercel_client_reads_the_tooling_marker() -> None:
    from app.connectors.vercel import VercelClient
    from tests.discovery_helpers import settings as dsettings

    routes = {
        "deployments": [
            {"uid": "a", "source": "redeploy", "meta": {"groundcrew:role": "tooling"}},
            {"uid": "b", "source": "redeploy", "meta": {"groundcrew_role": "Tooling"}},
            {"uid": "c", "source": "git", "meta": {}},
        ]
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=routes))
    client = VercelClient("t", None, dsettings(), transport=transport)
    import asyncio

    found = asyncio.run(client.production_deployments("prj_1"))
    assert [d["tooling"] for d in found] == [True, True, False]


# --- 4. first signal: the opening signal's onset --------------------------------------------------


async def test_the_detail_has_the_opening_signals_onset(
    session_factory: SessionFactory, client: httpx.AsyncClient, frank: Member
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, frank.workspace_id)
    onset = _now() - timedelta(minutes=2)
    async with sf() as session:
        opened = await open_incident(
            session,
            workspace_id=frank.workspace_id,
            resource_ids=[ids["worker"]],
            detected_by="watcher",
            signals=[
                Signal(
                    rule="log_error_spike",
                    resource_id=ids["worker"],
                    resource_name="checkout-worker",
                    detail="9 error lines",
                    at=onset,
                )
            ],
        )
        incident = await session.get_one(Incident, opened.incident.id)
        # Signals of an incident it took over are older (10:13 vs 10:57 live).
        incident.signals = [
            {**incident.signals[0], "at": (onset - timedelta(minutes=45)).isoformat()},
            *incident.signals,
        ]
        await session.commit()
    body = (await client.get(f"/incidents/{opened.incident.id}", headers=frank.headers)).json()
    assert datetime.fromisoformat(body["first_signal_at"].replace("Z", "+00:00")) == onset
