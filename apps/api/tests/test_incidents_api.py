"""Incidents API (Phase 5 M4): list with filters and paging, detail from Medic's state, PR state
refresh, and stopping a Medic run closes its incident."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.actions.base import ActionPlan
from app.agents.medic.events import ChangeMarker, ProposedFix, PullRequestView, Signal
from app.agents.medic.schemas import Claim, EvidenceItem, RootCause, TriggeringChange
from app.agents.medic.state import FixRecord, MedicState, MemoryUsed, Outcome
from app.config import get_settings
from app.db.models import Approval, ApprovalStatus, Incident, Run, RunStep, ToolPolicy
from app.db.session import SessionFactory, get_session_factory
from app.incidents.store import open_incident, set_status
from app.main import app
from app.pipeline.events import VerifyCheck, VerifyResult
from app.routes.incidents import get_github
from app.tools.github_pr import PullRequestDraft, PullRequestRef
from tests.discovery_helpers import INSTALLATION, Recorded, github_app
from tests.test_api import Member, _member, _settings
from tests.test_incidents_detection import _demo

pytestmark = pytest.mark.anyio

REPO = "GroundsCrew-AI/checkout-demo"


@pytest.fixture
async def client(session_factory: SessionFactory) -> AsyncIterator[httpx.AsyncClient]:
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = lambda: _settings()
    app.dependency_overrides[get_github] = lambda: None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def frank(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 6001, "frank")


@pytest.fixture
async def gina(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 6002, "gina")


async def _open(
    sf: SessionFactory,
    ws: uuid.UUID,
    rid: uuid.UUID,
    rule: str = "user_report",
    severity: Any = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    signals = (
        []
        if rule == "user_report"
        else [
            Signal(
                rule=rule,
                resource_id=rid,
                resource_name="svc",
                detail="d",
                at=datetime.now(UTC) - timedelta(minutes=3),
            )
        ]
    )
    async with sf() as session:
        opened = await open_incident(
            session,
            workspace_id=ws,
            resource_ids=[rid],
            detected_by="watcher" if signals else "user",
            signals=signals,
            severity=severity,
        )
    return opened.incident.id, opened.run.id


# --- list ---------------------------------------------------------------------------------------


async def test_list_filters_and_pages(
    client: httpx.AsyncClient, session_factory: SessionFactory, frank: Member
) -> None:
    ids = await _demo(session_factory, frank.workspace_id)
    first, run1 = await _open(
        session_factory, frank.workspace_id, ids["worker"], "ecs_tasks_stopped"
    )
    async with session_factory() as session:
        incident = await session.get(Incident, first)
        run = await session.get(Run, run1)
        assert incident and run
        await set_status(session, incident, run, "resolved")
        await session.commit()
    second, _ = await _open(session_factory, frank.workspace_id, ids["lonely"], severity="critical")
    base = f"/incidents?workspace_id={frank.workspace_id}"

    page = (await client.get(base, headers=frank.headers)).json()
    assert [i["id"] for i in page["items"]] == [str(second), str(first)] and page[
        "next_cursor"
    ] is None
    row = page["items"][1]
    assert row["status"] == "resolved" and row["detector_rule"] == "ecs_tasks_stopped"
    assert row["affected_resource_ids"] == [str(ids["worker"])]
    assert row["affected_resources"][0]["name"] == "checkout-worker"
    assert row["run_id"] == str(run1) and row["window"]["end"] is not None

    async def ids_for(query: str) -> list[str]:
        resp = await client.get(base + query, headers=frank.headers)
        assert resp.status_code == 200, resp.text
        return [i["id"] for i in resp.json()["items"]]

    assert await ids_for("&status=resolved") == [str(first)]
    assert await ids_for("&severity=critical") == [str(second)]
    assert await ids_for(f"&resource={ids['lonely']}") == [str(second)]
    assert await ids_for("&since=2999-01-01T00:00:00Z") == []

    one = (await client.get(base + "&limit=1", headers=frank.headers)).json()
    assert [i["id"] for i in one["items"]] == [str(second)] and one["next_cursor"]
    rest = (
        await client.get(base + f"&limit=1&cursor={one['next_cursor']}", headers=frank.headers)
    ).json()
    assert [i["id"] for i in rest["items"]] == [str(first)] and rest["next_cursor"] is None
    assert (await client.get(base + "&cursor=nonsense!", headers=frank.headers)).status_code == 422


# --- detail -------------------------------------------------------------------------------------


def _state(incident_id: uuid.UUID, approval_id: uuid.UUID, connection: uuid.UUID) -> MedicState:
    plan = ActionPlan(
        action="ecs_rollback_service",
        connection_id=connection,
        target="checkout/checkout-worker",
        params={"cluster": "checkout", "service": "checkout-worker"},
        current="td/checkout-worker:42",
        desired="td/checkout-worker:41",
        summary="Point checkout-worker back at :41",
        planned_at=datetime.now(UTC),
    )
    draft = PullRequestDraft(
        installation_id=INSTALLATION,
        repo=REPO,
        base="main",
        branch="groundcrew/fix-abc",
        title="Restore memory",
        body="b",
        files={"worker/ecs-task.json": "{}\n"},
    )
    item = EvidenceItem(
        id="ev-1111111111",
        source="ecs:checkout-worker",
        timestamp="2026-10-05T10:00:00+00:00",
        line="exit code 137 [OOM]",
    )
    return MedicState(
        phase="done",
        evidence=[item],
        changes=[
            ChangeMarker(kind="commit", ref="abc", at=datetime.now(UTC), label="abc Tune memory")
        ],
        root_cause=RootCause(
            summary="OOM since commit abc.",
            root_cause="Limit 24 MiB.",
            confidence="high",
            claims=[Claim(text="exit 137", evidence_ids=["ev-1111111111"])],
            triggering_change=TriggeringChange(kind="commit", ref="abc"),
        ),
        fixes=[
            FixRecord(
                fix=ProposedFix(
                    fix_id="fix-1",
                    kind="emergency",
                    order=1,
                    title="Roll back",
                    rationale="down",
                    action="ecs_rollback_service",
                    plan=plan,
                    approval_id=approval_id,
                ),
                status="applied",
                approval_id=approval_id,
            ),
            FixRecord(
                fix=ProposedFix(
                    fix_id="fix-2",
                    kind="pr",
                    order=2,
                    title="Restore memory",
                    rationale="r",
                    pull_request=PullRequestView(
                        repo=REPO,
                        base="main",
                        branch="groundcrew/fix-abc",
                        title="Restore memory",
                        body="b",
                        files=[],
                    ),
                ),
                status="applied",
                draft=draft,
                pull_request=PullRequestRef(
                    repo=REPO,
                    number=9,
                    url=f"https://github.com/{REPO}/pull/9",
                    branch="groundcrew/fix-abc",
                    head_sha="h",
                ),
            ),
        ],
        verify=VerifyResult(
            ok=True, checks=[VerifyCheck(name="rollout", ok=True, detail="1/1")], duration_ms=5
        ),
        memory=MemoryUsed(
            incident_id=uuid.uuid4(),
            title="last time",
            similarity=0.93,
            reused=["diagnosis", "fix"],
        ),
        outcome=Outcome(result="resolved", reason="Signals recovered"),
    )


async def _seed_detail(sf: SessionFactory, member: Member) -> tuple[uuid.UUID, uuid.UUID]:
    ids = await _demo(sf, member.workspace_id)
    incident_id, run_id = await _open(sf, member.workspace_id, ids["worker"], "ecs_tasks_stopped")
    async with sf() as session:
        incident = await session.get(Incident, incident_id)
        run = await session.get(Run, run_id)
        assert incident and run
        approval = Approval(
            run_id=run.id,
            workspace_id=member.workspace_id,
            tool_call_id="medic:x:fix-1",
            tool_name="ecs_rollback_service",
            policy=ToolPolicy.emergency,
            arguments={"plan_hash": "h"},
            status=ApprovalStatus.approved,
        )
        session.add(approval)
        await session.flush()
        incident.state = _state(incident.id, approval.id, ids["conn"]).model_dump(mode="json")
        run.prompt_tokens, run.completion_tokens, run.cost_usd = 1000, 200, Decimal("0.0042")
        second = Run(
            workspace_id=member.workspace_id,
            incident_id=incident.id,
            agent="medic",
            goal="again",
            prompt_tokens=10,
            completion_tokens=5,
            cost_usd=Decimal("0.0001"),
        )
        session.add(second)
        await set_status(session, incident, run, "resolved")
        await session.commit()
        return incident_id, second.id


async def test_detail_serves_medic_state_in_the_contract_shape(
    client: httpx.AsyncClient, session_factory: SessionFactory, frank: Member, gina: Member
) -> None:
    incident_id, latest = await _seed_detail(session_factory, frank)
    resp = await client.get(f"/incidents/{incident_id}", headers=frank.headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (
        body["status"] == "resolved" and body["run_id"] == str(latest) and len(body["run_ids"]) == 2
    )
    assert (body["prompt_tokens"], body["completion_tokens"], body["total_tokens"]) == (
        1010,
        205,
        1215,
    )
    assert body["cost_usd"] == pytest.approx(0.0043)
    assert body["root_cause"]["claims"][0]["evidence_ids"] == ["ev-1111111111"]
    assert body["evidence"][0]["id"] == "ev-1111111111"
    assert body["changes"][0]["kind"] == "commit"
    assert [f["fix"]["kind"] for f in body["fixes"]] == ["emergency", "pr"]
    assert body["fixes"][0]["approval_id"] and body["fixes"][1]["pull_request"]["state"] == "open"
    assert body["fixes_applied"] == ["ecs_rollback_service", "pull_request"]
    assert body["pull_requests"][0]["url"].endswith("/pull/9")
    assert body["memory"] == {
        "incident_id": body["memory"]["incident_id"],
        "title": "last time",
        "similarity": 0.93,
        "reused": ["diagnosis", "fix"],
    }
    assert body["memory_reused"] is True and body["outcome"]["result"] == "resolved"
    assert body["verify"]["ok"] is True and body["signals"][0]["rule"] == "ecs_tasks_stopped"
    assert body["time_to_detect_ms"] >= 0 and body["duration_ms"] is not None
    # The full PR file contents (the draft) are not part of the API.
    assert "draft" not in body["fixes"][1]
    assert (await client.get(f"/incidents/{incident_id}", headers=gina.headers)).status_code == 404


async def test_detail_refreshes_pr_state_at_most_once_a_minute(
    client: httpx.AsyncClient, session_factory: SessionFactory, frank: Member
) -> None:
    incident_id, _ = await _seed_detail(session_factory, frank)
    recorded = Recorded()
    gh = github_app(recorded, {f"GET /repos/{REPO}/pulls/9": {"merged": True, "state": "closed"}})
    app.dependency_overrides[get_github] = lambda: gh
    body = (await client.get(f"/incidents/{incident_id}", headers=frank.headers)).json()
    assert body["fixes"][1]["pull_request"]["state"] == "merged"
    assert body["pull_requests"][0]["state"] == "merged"
    calls = [r for r in recorded.requests if r.url.path.endswith("/pulls/9")]
    await client.get(f"/incidents/{incident_id}", headers=frank.headers)
    assert len([r for r in recorded.requests if r.url.path.endswith("/pulls/9")]) == len(calls) == 1


# --- stop ---------------------------------------------------------------------------------------


async def test_stopping_the_medic_run_closes_the_incident(
    client: httpx.AsyncClient, session_factory: SessionFactory, frank: Member
) -> None:
    ids = await _demo(session_factory, frank.workspace_id)
    incident_id, run_id = await _open(session_factory, frank.workspace_id, ids["api"])
    resp = await client.post(f"/runs/{run_id}/stop", headers=frank.headers)
    assert resp.status_code == 200, resp.text
    async with session_factory() as session:
        incident = await session.get(Incident, incident_id)
        assert incident is not None and incident.status == "failed"
        assert MedicState.model_validate(incident.state).outcome == Outcome(
            result="failed", reason="Stopped by frank."
        )
        kinds = list(
            await session.scalars(
                select(RunStep.kind).where(RunStep.run_id == run_id).order_by(RunStep.seq)
            )
        )
        assert kinds == ["incident_opened", "stopped", "incident_updated", "incident_failed"]
    body = (await client.get(f"/incidents/{incident_id}", headers=frank.headers)).json()
    assert body["status"] == "failed" and body["outcome"]["reason"] == "Stopped by frank."


# --- test incidents ------------------------------------------------------------------------------


async def test_a_test_report_stays_out_of_memory_and_apart_from_real_incidents(
    client: httpx.AsyncClient, session_factory: SessionFactory, frank: Member
) -> None:
    from app.agents.medic import memory as mem

    ids = await _demo(session_factory, frank.workspace_id)
    body = {
        "workspace_id": str(frank.workspace_id),
        "resource_ids": [str(ids["api"])],
        "description": "Live-update check from a dev session",
        "test": True,
    }
    created = await client.post("/incidents", json=body, headers=frank.headers)
    assert created.status_code == 202, created.text
    test_id = uuid.UUID(created.json()["incident_id"])
    detail = (await client.get(f"/incidents/{test_id}", headers=frank.headers)).json()
    assert detail["test"] is True

    # A real signal on the same service opens its own incident, not the open test one.
    real, _ = await _open(session_factory, frank.workspace_id, ids["api"], "health_check")
    assert real != test_id
    page = (
        await client.get(f"/incidents?workspace_id={frank.workspace_id}", headers=frank.headers)
    ).json()
    assert {(i["id"], i["test"]) for i in page["items"]} == {
        (str(test_id), True),
        (str(real), False),
    }

    async with session_factory() as session:
        test = await session.get(Incident, test_id)
        incident = await session.get(Incident, real)
        assert test is not None and incident is not None
        test.status, test.resolved_at = "resolved", datetime.now(UTC)
        # Remembering a test incident stores no embedding.
        mem.remember(test, "false alarm", "a dev check", [0.1] * get_settings().embed_dim)
        assert test.embedding is None
        await session.commit()
        # Neither the embedding nor the rules/resources fallback finds it.
        assert await mem.similar(session, incident, None) == []
        assert await mem.similar(session, incident, [0.1] * get_settings().embed_dim) == []
