"""Retrying a failed emergency fix (live bad-deploy-oom: the rollback failed with AccessDenied on
iam:PassRole). The failure stays visible with AWS's error; a retry plans again and needs a new
approval on the same run; nothing runs without it."""

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.actions.base import ActionPlan, ActionResult, EmergencyActionError
from app.actions.fake import FakeEmergencyActions, FakeSecret, FakeService
from app.agents.medic.jobs import investigate_incident_job
from app.agents.medic.retry import RetryError, retry_fix
from app.db.models import Approval, ApprovalStatus, Incident, RunStatus, Workspace
from app.db.session import SessionFactory
from app.llm.fake import FakeLLM, reply
from app.main import app
from app.pipeline.resume import resume_run_job
from app.policy.actions import ApprovedAction
from app.policy.engine import Decision
from app.routes.incidents import get_actions
from tests.test_api import Member, _member
from tests.test_incidents_api import client as api_client
from tests.test_medic import (
    BAD_SHA,
    BAD_TASK,
    SECRET_ARN,
    TD,
    Harness,
    _decide,
    _open,
    _state,
    demo_graph,
    eid,
    env_evidence,
    github_routes,
    oom_evidence,
    oom_root_cause,
    settings,
)
from tests.test_medic import _now as now
from tests.test_medic import oom_fix as oom_fix_reply
from tests.test_medic import use_deps as medic_deps

pytestmark = pytest.mark.anyio

# Shared fixtures, under this module's names.
use_deps = medic_deps
client = api_client


@pytest.fixture
async def frank(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 6101, "frank-retry")


DENIED = (
    "AccessDeniedException: User: arn:aws:sts::881050425883:assumed-role/groundcrew-operate/"
    "groundcrew-operate is not authorized to perform: iam:PassRole on resource: "
    "arn:aws:iam::881050425883:role/checkout-worker-task-execution"
)


class DeniedOnce(FakeEmergencyActions):
    """AWS refuses execute (the token is still spent) until `denied` is cleared."""

    denied: str | None = DENIED

    async def execute(
        self, plan: ActionPlan, token: ApprovedAction, *, run_id: uuid.UUID
    ) -> ActionResult:
        if self.denied is None:
            return await super().execute(plan, token, run_id=run_id)
        at = datetime.now(UTC)
        return ActionResult(
            action=plan.action,
            ok=False,
            summary="AWS refused the action.",
            error=self.denied,
            started_at=at,
            finished_at=at,
        )


async def _oom(
    sf: SessionFactory, workspace: Workspace, use_deps: Any
) -> tuple[uuid.UUID, uuid.UUID, DeniedOnce]:
    ids = await demo_graph(sf, workspace.id)
    ev = oom_evidence(ids, now())
    llm = FakeLLM(reply(oom_root_cause(ev)), reply(oom_fix_reply()), settings=settings())
    actions = DeniedOnce(
        sf,
        services={
            "checkout/checkout-worker": FakeService(
                revisions=[f"{TD}/checkout-worker:41", f"{TD}/checkout-worker:42"]
            ),
        },
    )
    use_deps(
        Harness(
            sf,
            ev,
            llm,
            actions=actions,
            gh=github_routes(BAD_SHA, {"worker/ecs-task.json": ("+x", BAD_TASK)}),
        )
    )
    incident_id, run_id = await _open(
        sf, workspace.id, ids["worker"], "ecs_tasks_stopped", "3 tasks exit 137"
    )
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})
    return incident_id, run_id, actions


async def _approve(sf: SessionFactory, ws: Workspace, run_id: uuid.UUID, aid: Any) -> None:
    await _decide(sf, ws, aid, Decision.approve)
    await resume_run_job(sf, {"run_id": str(run_id), "approval_id": str(aid)})


async def _retry(sf: SessionFactory, incident_id: uuid.UUID, fix_id: str, actions: Any) -> None:
    async with sf() as session:
        incident = await session.get_one(Incident, incident_id)
        await retry_fix(session, incident, fix_id, actions, now=now())


async def test_a_failed_rollback_keeps_the_aws_error_and_retries_with_a_new_approval(
    session_factory: SessionFactory, workspace: Workspace, use_deps: Any
) -> None:
    sf = session_factory
    incident_id, run_id, actions = await _oom(sf, workspace, use_deps)
    _, state, _ = await _state(sf, incident_id)
    rollback, pr = state.fixes
    await _approve(sf, workspace, run_id, rollback.approval_id)
    await _approve(sf, workspace, run_id, pr.approval_id)
    incident, state, run = await _state(sf, incident_id)
    assert [f.status for f in state.fixes] == ["failed", "applied"]
    assert state.fixes[0].applied is not None and state.fixes[0].applied.error == DENIED
    assert incident.status == "verifying"  # the PR was opened; verify waits for its deploy
    old_approval = state.fixes[0].approval_id

    # Retrying before the fix failed, or a PR fix, is refused.
    with pytest.raises(RetryError, match="Only a failed emergency fix"):
        await _retry(sf, incident_id, "fix-2", actions)

    # The stack now has OperatePassRoleArns: retry, which needs a new approval.
    actions.denied = None
    await _retry(sf, incident_id, "fix-1", actions)
    incident, state, run = await _state(sf, incident_id)
    retried = state.fixes[0]
    assert retried.status == "waiting_approval" and retried.applied is None
    assert retried.approval_id not in (None, old_approval)
    assert incident.status == "waiting_approval" and run.status == RunStatus.waiting_approval
    assert run.finished_at is None and state.phase == "waiting_approval"
    assert actions.executed == []  # nothing ran without the new approval
    async with sf() as session:
        new = await session.get_one(Approval, retried.approval_id)
        assert new.status == ApprovalStatus.pending and new.tool_call_id.endswith(":fix-1:retry-1")
        assert new.arguments["plan_hash"] == retried.fix.plan.plan_hash()  # type: ignore[union-attr]

    await _approve(sf, workspace, run_id, retried.approval_id)
    incident, state, run = await _state(sf, incident_id)
    assert [f.status for f in state.fixes] == ["applied", "applied"]
    assert actions.services["checkout/checkout-worker"].revisions[-1].endswith(":41")
    assert incident.status == "verifying"

    # An applied fix is not retried again.
    with pytest.raises(RetryError):
        await _retry(sf, incident_id, "fix-1", actions)


async def test_a_retry_reopens_a_failed_run_and_the_restarts_it_held_back(
    session_factory: SessionFactory, workspace: Workspace, use_deps: Any
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    ev = env_evidence(ids, now())
    secret_line = eid(ev, "secrets:checkout/database-url", "last changed")
    rc = (
        '{"summary": "DATABASE_URL changed.", "root_cause": "checkout/database-url changed.",'
        f' "claims": [{{"text": "Secret changed.", "evidence_ids": ["{secret_line}"]}}],'
        f' "triggering_change": {{"kind": "secret_change", "ref": "{SECRET_ARN}:v-bad",'
        f' "evidence_ids": ["{secret_line}"]}}, "affected_resources": ["checkout-api"],'
        ' "confidence": "high", "fix_options": []}'
    )
    actions = DeniedOnce(
        sf,
        services={
            "checkout/checkout-api": FakeService(revisions=[f"{TD}/checkout-api:17"]),
            "checkout/checkout-worker": FakeService(revisions=[f"{TD}/checkout-worker:41"]),
        },
        secrets={
            SECRET_ARN: FakeSecret(versions={"v-good": ["AWSPREVIOUS"], "v-bad": ["AWSCURRENT"]})
        },
    )
    use_deps(Harness(sf, ev, FakeLLM(reply(rc), settings=settings()), actions=actions))
    incident_id, run_id = await _open(sf, workspace.id, ids["api"], "health_check", "unhealthy")
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})
    _, state, _ = await _state(sf, incident_id)
    await _approve(sf, workspace, run_id, state.fixes[0].approval_id)
    incident, state, run = await _state(sf, incident_id)
    assert [f.status for f in state.fixes] == ["failed", "rejected", "rejected"]
    assert incident.status == "failed" and run.status in (RunStatus.succeeded, RunStatus.failed)

    actions.denied = None
    await _retry(sf, incident_id, "fix-1", actions)
    incident, state, run = await _state(sf, incident_id)
    assert [f.status for f in state.fixes] == ["waiting_approval"] * 3
    assert incident.status == "waiting_approval" and run.status == RunStatus.waiting_approval
    assert state.outcome is None and state.error is None
    for fix in state.fixes:
        await _approve(sf, workspace, run_id, fix.approval_id)
    incident, state, _ = await _state(sf, incident_id)
    assert [f.status for f in state.fixes] == ["applied"] * 3
    assert [p.action for p in actions.executed] == [
        "secret_rollback_version",
        "force_redeploy",
        "force_redeploy",
    ]
    assert incident.status == "verifying"


async def test_retry_route(
    session_factory: SessionFactory, use_deps: Any, client: httpx.AsyncClient, frank: Member
) -> None:
    """POST /incidents/{id}/fixes/{fix_id}/retry: 404 for an unknown fix, 409 for one that did
    not fail, 422 with the plan's reason (still no PassRole) and nothing changed, then the
    incident with the new approval."""
    sf = session_factory
    async with sf() as session:
        workspace = await session.get_one(Workspace, frank.workspace_id)
    incident_id, run_id, actions = await _oom(sf, workspace, use_deps)
    _, state, _ = await _state(sf, incident_id)
    for fix in state.fixes:
        await _approve(sf, workspace, run_id, fix.approval_id)

    refusals = [
        "rollback needs PassRole on arn:aws:iam::881050425883:role/"
        "checkout-worker-task-execution; update the connection"
    ]

    class Planner(FakeEmergencyActions):
        async def plan_ecs_rollback_service(
            self, connection_id: uuid.UUID, cluster: str, service: str
        ) -> ActionPlan:
            if refusals:
                raise EmergencyActionError(refusals.pop())
            return await super().plan_ecs_rollback_service(connection_id, cluster, service)

    app.dependency_overrides[get_actions] = lambda: Planner(sf, services=actions.services)
    base = f"/incidents/{incident_id}/fixes"
    r = await client.post(f"{base}/fix-9/retry", headers=frank.headers)
    assert r.status_code == 404, r.text
    r = await client.post(f"{base}/fix-2/retry", headers=frank.headers)
    assert r.status_code == 409, r.text
    r = await client.post(f"{base}/fix-1/retry", headers=frank.headers)
    assert r.status_code == 422 and "needs PassRole" in r.json()["detail"]
    _, state, _ = await _state(sf, incident_id)
    assert state.fixes[0].status == "failed"  # a refused plan changes nothing
    r = await client.post(f"{base}/fix-1/retry", headers=frank.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "waiting_approval"
    assert body["fixes"][0]["status"] == "waiting_approval"
    async with sf() as session:
        pending = await session.scalars(
            select(Approval).where(
                Approval.run_id == run_id, Approval.status == ApprovalStatus.pending
            )
        )
        assert [a.id for a in pending] == [uuid.UUID(body["fixes"][0]["approval_id"])]


async def test_tavily_keeps_one_result_per_url() -> None:
    """Live bad-deploy-oom: Tavily returned one runbook twice, which gave the incident page
    duplicate React keys (the "4 issues" dev badge)."""
    from tests.test_medic import tavily

    page = {"title": "OOMKilled", "url": "https://example.com/oom", "content": "a"}
    found = await tavily([page, {**page, "content": "b"}, {**page, "url": "https://x.io"}]).search(
        "oom"
    )
    assert [r.url for r in found] == ["https://example.com/oom", "https://x.io"]
