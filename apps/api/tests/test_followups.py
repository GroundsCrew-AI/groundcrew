"""Follow-ups: an incident resolved by a rollback while its fix PR is still open (live
bad-deploy-oom: checkout-demo PR #3). The PR is tracked read-only until it is deployed or
closed; memory takes the incident only once the PR is merged (or as "rollback only")."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.agents.medic import memory as mem
from app.agents.medic.events import ChangeMarker
from app.agents.medic.state import MedicState
from app.db.models import Connection, Incident, IncidentFollowUp, Run, RunStep
from app.db.session import SessionFactory
from app.incidents import followups as fu
from tests.discovery_helpers import INSTALLATION, Recorded, github_app
from tests.test_api import Member, _member
from tests.test_incidents_api import REPO, _open, _state
from tests.test_incidents_api import client as api_client
from tests.test_incidents_detection import _demo

pytestmark = pytest.mark.anyio

client = api_client
BAD = "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432"
MERGE = "a" * 40
T0 = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
PR = f"GET /repos/{REPO}/pulls/9"
RUNS = f"GET /repos/{REPO}/actions/runs"


@pytest.fixture
async def frank(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 6201, "frank-followups")


async def _resolved(
    sf: SessionFactory,
    ws: uuid.UUID,
    pr_state: str = "open",
    ids: dict[str, uuid.UUID] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """A resolved incident: rollback applied, fix PR #9 opened (pr_state), follow-ups created
    as Medic's resolve does."""
    ids = ids or await _demo(sf, ws)
    incident_id, run_id = await _open(sf, ws, ids["worker"], "ecs_tasks_stopped")
    async with sf() as session:
        incident = await session.get_one(Incident, incident_id)
        state = _state(incident_id, uuid.uuid4(), ids["conn"])
        rc = state.root_cause
        assert rc is not None
        state.root_cause = rc.model_copy(
            update={
                "triggering_change": rc.triggering_change.model_copy(
                    update={"kind": "commit", "ref": BAD}
                )
                if rc.triggering_change
                else None
            }
        )
        pr = state.fixes[1].pull_request
        assert pr is not None
        state.fixes[1].pull_request = pr.model_copy(update={"state": pr_state})
        incident.state = state.model_dump(mode="json")
        incident.status, incident.resolved_at = "resolved", T0
        created = await fu.create_on_resolve(session, incident, state, T0)
        fu.remember_if_settled(incident, any(f.state == "open" for f in created), T0)
        await session.commit()
    return incident_id, run_id


async def _followups(sf: SessionFactory, incident_id: uuid.UUID) -> list[IncidentFollowUp]:
    async with sf() as session:
        return list(
            await session.scalars(
                select(IncidentFollowUp).where(IncidentFollowUp.incident_id == incident_id)
            )
        )


async def _incident(sf: SessionFactory, incident_id: uuid.UUID) -> Incident:
    async with sf() as session:
        return await session.get_one(Incident, incident_id)


async def test_resolving_with_an_open_pr_creates_one_followup_and_holds_memory(
    session_factory: SessionFactory, frank: Member
) -> None:
    sf = session_factory
    incident_id, _ = await _resolved(sf, frank.workspace_id)
    (row,) = await _followups(sf, incident_id)
    assert (row.state, row.repo, row.number, row.triggering_commit) == ("open", REPO, 9, BAD)
    assert (row.base, row.installation_id) == ("main", INSTALLATION)
    incident = await _incident(sf, incident_id)
    assert incident.remembered_at is None
    async with sf() as session:
        # Idempotent: a second resolve adds nothing.
        incident = await session.get_one(Incident, incident_id)
        state = MedicState.model_validate(incident.state)
        assert await fu.create_on_resolve(session, incident, state, T0) == []
        # Memory cannot find it while the PR is open.
        assert await mem.similar(session, incident_like(incident), None) == []


def incident_like(past: Incident) -> Incident:
    """A new incident with the same signals and resources (memory's fallback match)."""
    return Incident(
        id=uuid.uuid4(),
        workspace_id=past.workspace_id,
        title=past.title,
        signals=past.signals,
        affected_resource_ids=past.affected_resource_ids,
    )


async def test_merged_then_deployed_and_checked_at_most_every_two_minutes(
    session_factory: SessionFactory, frank: Member
) -> None:
    sf = session_factory
    incident_id, run_id = await _resolved(sf, frank.workspace_id)
    recorded = Recorded()
    merged = {
        PR: {
            "merged": True,
            "state": "closed",
            "merged_at": "2026-10-14T10:05:00Z",
            "merge_commit_sha": MERGE,
        }
    }
    app = github_app(recorded, merged)
    assert await fu.track_followups(sf, app, now=T0 + timedelta(minutes=10)) == 1
    (row,) = await _followups(sf, incident_id)
    assert (row.state, row.merge_commit) == ("merged", MERGE)
    incident = await _incident(sf, incident_id)
    assert incident.remembered_at is not None  # memory now has the merged fix
    state = MedicState.model_validate(incident.state)
    assert state.fixes[1].pull_request and state.fixes[1].pull_request.state == "merged"
    async with sf() as session:
        (hit,) = await mem.similar(session, incident_like(incident), None)
        assert hit.fix_summary == "Roll back; Restore memory"

    # Within two minutes nothing is asked again.
    before = len(recorded.requests)
    assert await fu.track_followups(sf, app, now=T0 + timedelta(minutes=11)) == 0
    assert len(recorded.requests) == before

    # No deploy yet: still merged.
    later = github_app(extra_routes={**merged, RUNS: {"workflow_runs": []}})
    assert await fu.track_followups(sf, later, now=T0 + timedelta(minutes=13)) == 0
    # A later deploy whose head contains the merge commit: deployed.
    head = "b" * 40
    deploy = {
        RUNS: {
            "workflow_runs": [
                {"path": ".github/workflows/ci.yml", "conclusion": "success", "head_sha": MERGE},
                {
                    "path": ".github/workflows/deploy.yml",
                    "conclusion": "success",
                    "head_sha": head,
                    "updated_at": "2026-10-14T10:20:00Z",
                    "html_url": f"https://github.com/{REPO}/actions/runs/7",
                },
            ]
        },
        f"GET /repos/{REPO}/compare/{MERGE}...{head}": {"status": "ahead"},
    }
    seen = Recorded()
    deployed_app = github_app(seen, deploy)
    assert await fu.track_followups(sf, deployed_app, now=T0 + timedelta(minutes=16)) == 1
    (row,) = await _followups(sf, incident_id)
    assert row.state == "deployed" and row.deploy_url.endswith("/runs/7")  # type: ignore[union-attr]
    runs_call = next(r for r in seen.requests if r.url.path.endswith("/actions/runs"))
    assert runs_call.url.params["branch"] == "main"
    assert runs_call.url.params["created"] == ">=2026-10-14T10:05:00Z"
    # Read-only on the repository (the one POST is the App's installation token).
    assert all(r.method == "GET" for r in seen.requests if r.url.path.startswith("/repos/"))
    # Done: never checked again.
    assert await fu.track_followups(sf, deployed_app, now=T0 + timedelta(hours=1)) == 0

    async with sf() as session:
        steps = list(
            await session.scalars(
                select(RunStep).where(RunStep.run_id == run_id, RunStep.kind == "incident_updated")
            )
        )
    reasons = [s.data["reason"] for s in steps if s.data and s.data.get("followups")]
    assert reasons == [
        f"Permanent fix: {REPO}#9 merged, waiting for its deploy",
        f"Permanent fix: {REPO}#9 deployed",
    ]
    last = steps[-1].data
    assert last is not None and last["status"] == "resolved"
    assert last["followups"][0]["state"] == "deployed"


async def test_closed_without_merge_warns_and_memory_says_rollback_only(
    session_factory: SessionFactory, frank: Member
) -> None:
    sf = session_factory
    incident_id, _ = await _resolved(sf, frank.workspace_id)
    closed = {PR: {"merged": False, "state": "closed", "closed_at": "2026-10-14T11:00:00Z"}}
    assert (
        await fu.track_followups(sf, github_app(extra_routes=closed), now=T0 + timedelta(hours=1))
        == 1
    )
    (row,) = await _followups(sf, incident_id)
    assert row.state == "closed_unmerged" and row.triggering_commit == BAD
    incident = await _incident(sf, incident_id)
    assert incident.remembered_at is not None
    async with sf() as session:
        (hit,) = await mem.similar(session, incident_like(incident), None)
    assert hit.fix_summary == "Roll back (rollback only: the fix PR was closed without merging)"


async def test_tracking_stops_after_seven_days(
    session_factory: SessionFactory, frank: Member
) -> None:
    sf = session_factory
    await _resolved(sf, frank.workspace_id)
    recorded = Recorded()
    app = github_app(recorded, {PR: {"merged": True, "merge_commit_sha": MERGE}})
    assert await fu.track_followups(sf, app, now=T0 + timedelta(days=7, minutes=1)) == 0
    assert recorded.requests == []
    assert await fu.track_followups(sf, None) == 0  # no GitHub App configured


async def test_merged_before_resolve_is_tracked_until_deployed(
    session_factory: SessionFactory, frank: Member
) -> None:
    incident_id, _ = await _resolved(session_factory, frank.workspace_id, pr_state="merged")
    (row,) = await _followups(session_factory, incident_id)
    assert row.state == "merged" and row.merge_commit is None
    assert (await _incident(session_factory, incident_id)).remembered_at == T0
    # The tracker learns the merge commit from the PR, then finds its deploy.
    routes = {
        PR: {"merged": True, "merged_at": "2026-10-14T09:50:00Z", "merge_commit_sha": MERGE},
        RUNS: {
            "workflow_runs": [
                {"path": ".github/workflows/deploy.yml", "conclusion": "success", "head_sha": MERGE}
            ]
        },
    }
    app = github_app(extra_routes=routes)
    assert await fu.track_followups(session_factory, app, now=T0 + timedelta(minutes=5)) == 1
    (row,) = await _followups(session_factory, incident_id)
    assert (row.state, row.merge_commit) == ("deployed", MERGE)


async def test_list_flag_filter_detail_and_overview_count(
    session_factory: SessionFactory, client: httpx.AsyncClient, frank: Member
) -> None:
    sf = session_factory
    ids = await _demo(sf, frank.workspace_id)
    pending_id, _ = await _resolved(sf, frank.workspace_id, ids=ids)
    done_id, _ = await _resolved(sf, frank.workspace_id, pr_state="closed", ids=ids)
    params: dict[str, Any] = {"workspace_id": str(frank.workspace_id)}
    r = await client.get("/incidents", params=params, headers=frank.headers)
    flags = {i["id"]: i["has_open_followups"] for i in r.json()["items"]}
    assert flags == {str(pending_id): True, str(done_id): False}
    r = await client.get(
        "/incidents", params={**params, "pending_fix": "true"}, headers=frank.headers
    )
    assert [i["id"] for i in r.json()["items"]] == [str(pending_id)]
    r = await client.get(
        "/incidents", params={**params, "pending_fix": "false"}, headers=frank.headers
    )
    assert [i["id"] for i in r.json()["items"]] == [str(done_id)]
    r = await client.get(f"/incidents/{pending_id}", headers=frank.headers)
    body = r.json()
    assert body["has_open_followups"] is True
    (followup,) = body["followups"]
    assert followup["state"] == "open" and followup["number"] == 9
    assert followup["triggering_commit"] == BAD and followup["kind"] == "fix_pr"
    r = await client.get(f"/workspaces/{frank.workspace_id}/summary", headers=frank.headers)
    assert r.json()["pending_permanent_fixes"] == 1


async def test_medic_resolve_records_the_followup_on_its_run(
    session_factory: SessionFactory, frank: Member
) -> None:
    """The resolve path itself (MedicRunner._resolve) is covered end to end in
    test_medic.test_bad_deploy_oom_end_to_end; here: the step it records."""
    sf = session_factory
    incident_id, run_id = await _resolved(sf, frank.workspace_id)
    async with sf() as session:
        incident = await session.get_one(Incident, incident_id)
        run = await session.get_one(Run, run_id)
        await fu.record_change(session, incident, run, f"Permanent fix pending: {REPO}#9", T0)
        await session.commit()
        step = await session.scalar(
            select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.seq.desc())
        )
    assert step is not None and step.data is not None
    assert step.data["type"] == "incident_updated" and step.data["followups"][0]["number"] == 9


# --- change_in_repo: rolled back, no fix PR on its way -------------------------------------------

PARENT = "c" * 40
OLDER, NEWEST = "d" * 40, "e" * 40
TASK = "worker/ecs-task.json"


async def _rolled_back(sf: SessionFactory, ws: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Resolved by the rollback; the fix PR was rejected. The workspace's GitHub connection
    lists the repo."""
    ids = await _demo(sf, ws)
    incident_id, run_id = await _open(sf, ws, ids["worker"], "ecs_tasks_stopped")
    async with sf() as session:
        session.add(
            Connection(
                workspace_id=ws,
                kind="github",
                name="GroundsCrew-AI",
                status="connected",
                config={"installation_id": INSTALLATION, "repos": [REPO]},
            )
        )
        incident = await session.get_one(Incident, incident_id)
        state = _state(incident_id, uuid.uuid4(), ids["conn"])
        rc = state.root_cause
        assert rc is not None and rc.triggering_change is not None
        state.root_cause = rc.model_copy(
            update={
                "triggering_change": rc.triggering_change.model_copy(
                    update={"kind": "commit", "ref": BAD[:7]}
                )
            }
        )
        state.changes = [
            ChangeMarker(kind="commit", ref=BAD, at=T0, label="cap memory", commit=BAD, repo=REPO)
        ]
        pr_fix = state.fixes[1]
        state.fixes[1] = pr_fix.model_copy(update={"status": "rejected", "pull_request": None})
        incident.state = state.model_dump(mode="json")
        incident.status, incident.resolved_at = "resolved", T0
        created = await fu.create_on_resolve(session, incident, state, T0)
        fu.remember_if_settled(incident, fu.holds_memory(created), T0)
        await session.commit()
    return incident_id, run_id


def _deploys(*heads: tuple[str, str]) -> dict[str, Any]:
    return {
        "workflow_runs": [
            {
                "path": ".github/workflows/deploy.yml",
                "conclusion": "success",
                "head_sha": head,
                "created_at": created,
                "updated_at": created,
                "html_url": f"https://github.com/{REPO}/actions/runs/{head[:4]}",
            }
            for head, created in heads
        ]
    }


def _github(blob_at_newest: str) -> Any:
    """GitHub for the revert check: the bad commit changed one file; at its parent the file is
    "good"; at the newest deploy's head it is `blob_at_newest`."""

    def contents(request: httpx.Request) -> dict[str, str]:
        ref = request.url.params["ref"]
        return {"sha": "good" if ref == PARENT else blob_at_newest if ref == NEWEST else "bad"}

    return github_app(
        extra_routes={
            f"GET /repos/{REPO}": {"default_branch": "main"},
            RUNS: _deploys((OLDER, "2026-10-14T10:30:00Z"), (NEWEST, "2026-10-14T11:00:00Z")),
            f"GET /repos/{REPO}/commits/{BAD}": {
                "sha": BAD,
                "parents": [{"sha": PARENT}],
                "files": [{"filename": TASK, "status": "modified"}],
            },
            f"GET /repos/{REPO}/contents/{TASK}": contents,
        }
    )


async def test_a_rollback_without_a_fix_pr_tracks_the_change_still_in_the_repo(
    session_factory: SessionFactory, frank: Member
) -> None:
    sf = session_factory
    incident_id, run_id = await _rolled_back(sf, frank.workspace_id)
    (row,) = await _followups(sf, incident_id)
    assert (row.kind, row.state, row.repo, row.triggering_commit) == (
        "change_in_repo",
        "open",
        REPO,
        BAD,
    )
    assert (row.installation_id, row.number, row.base) == (INSTALLATION, None, "")
    # The rollback was really applied: memory may use the incident now.
    assert (await _incident(sf, incident_id)).remembered_at == T0

    # The newest deploy still has the change: still open.
    assert await fu.track_followups(sf, _github("bad"), now=T0 + timedelta(minutes=5)) == 0
    (row,) = await _followups(sf, incident_id)
    assert (row.state, row.base) == ("open", "main")
    # A newer deploy whose diff reverts it (the file is as before the commit): reverted.
    assert await fu.track_followups(sf, _github("good"), now=T0 + timedelta(minutes=8)) == 1
    (row,) = await _followups(sf, incident_id)
    assert (
        row.state == "reverted" and row.deploy_url == f"https://github.com/{REPO}/actions/runs/eeee"
    )
    async with sf() as session:
        step = await session.scalar(
            select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.seq.desc())
        )
    assert step is not None and step.data is not None
    assert step.data["reason"] == f"Commit {BAD[:7]} is no longer in the deployed code (reverted)"


async def test_no_change_in_repo_followup_with_a_fix_pr_or_without_a_rollback(
    session_factory: SessionFactory, frank: Member
) -> None:
    sf = session_factory
    ids = await _demo(sf, frank.workspace_id)
    with_pr, _ = await _resolved(sf, frank.workspace_id, ids=ids)
    assert [r.kind for r in await _followups(sf, with_pr)] == ["fix_pr"]
    async with sf() as session:
        incident = await session.get_one(Incident, with_pr)
        state = MedicState.model_validate(incident.state)
        state.fixes = [r for r in state.fixes if r.fix.kind != "emergency"]
        state.fixes[0] = state.fixes[0].model_copy(update={"status": "rejected"})
        other = Incident(workspace_id=frank.workspace_id, title="no rollback", status="resolved")
        session.add(other)
        await session.flush()
        assert await fu.create_on_resolve(session, other, state, T0) == []


async def test_a_person_dismisses_the_change_in_repo(
    session_factory: SessionFactory, client: httpx.AsyncClient, frank: Member
) -> None:
    sf = session_factory
    incident_id, _ = await _rolled_back(sf, frank.workspace_id)
    (row,) = await _followups(sf, incident_id)
    base = f"/incidents/{incident_id}/followups"
    r = await client.post(f"{base}/{uuid.uuid4()}/dismiss", headers=frank.headers)
    assert r.status_code == 404
    r = await client.post(f"{base}/{row.id}/dismiss", headers=frank.headers)
    assert r.status_code == 200, r.text
    (followup,) = r.json()["followups"]
    assert followup["state"] == "dismissed" and followup["dismissed_by"] == "frank-followups"
    assert r.json()["has_open_followups"] is False
    r = await client.post(f"{base}/{row.id}/dismiss", headers=frank.headers)
    assert r.status_code == 409
    # A dismissed follow-up is never checked again.
    assert await fu.track_followups(sf, _github("good"), now=T0 + timedelta(minutes=5)) == 0


async def test_only_change_in_repo_can_be_dismissed(
    session_factory: SessionFactory, client: httpx.AsyncClient, frank: Member
) -> None:
    incident_id, _ = await _resolved(session_factory, frank.workspace_id)
    (row,) = await _followups(session_factory, incident_id)
    r = await client.post(
        f"/incidents/{incident_id}/followups/{row.id}/dismiss", headers=frank.headers
    )
    assert r.status_code == 409
