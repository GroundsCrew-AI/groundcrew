"""Phase 5 contract (M0): incident API shapes behind auth, incident events on the run stream,
the RootCause schema, and the fakes for emergency actions and fix PRs with real approvals."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, get_args

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.actions.base import EMERGENCY_ACTION_NAMES, ActionPlan
from app.actions.fake import FakeEmergencyActions
from app.agents.medic.events import (
    MEDIC_EVENT_NAMES,
    Diagnosis,
    FixApproval,
    IncidentOpened,
    ProposedFix,
    ResourceRef,
    Signal,
)
from app.agents.medic.schemas import EvidenceRef, RootCause, TriggeringChange
from app.config import get_settings
from app.db.models import Run, RunStatus, RunStep, Workspace
from app.db.session import SessionFactory, get_session_factory
from app.llm.structured import choices_without_other
from app.main import app
from app.pipeline.events import DEPLOY_EVENT_NAMES, VerifyCheck, VerifyResult
from app.policy.actions import ApprovedActionName, MedicAction
from app.policy.engine import PolicyViolationError
from app.routes.incident_schemas import IncidentCreate
from app.routes.schemas import SseEventName, StepOut
from app.steps import record_step
from app.telemetry.evidence import FakeEvidence, evidence_line
from app.tools.github_pr import FakeFixPullRequests, PullRequestDraft, check_fix_branch
from tests.medic_fakes import (
    SECRET_ARN,
    TASK_DEF,
    approve_medic_action,
    checkout_secrets,
    checkout_services,
    medic_run,
)
from tests.test_api import Member, _member, _parse_sse, _settings, _stream_url

pytestmark = pytest.mark.anyio

CONNECTION = uuid.uuid4()
NOW = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)


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
    return await _member(session_factory, 3001, "alice")


@pytest.fixture
async def bob(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 3002, "bob")


# --- request schema -----------------------------------------------------------------------


def _create(**overrides: Any) -> dict[str, Any]:
    return {"workspace_id": str(uuid.uuid4()), "resource_ids": [str(uuid.uuid4())], **overrides}


def test_incident_create_needs_resources_and_rejects_unknown_fields() -> None:
    assert IncidentCreate.model_validate(_create()).since is None
    for bad in (
        _create(resource_ids=[]),
        _create(resource_ids=[str(uuid.uuid4()) for _ in range(21)]),
        _create(region="us-east-1"),
    ):
        with pytest.raises(ValidationError):
            IncidentCreate.model_validate(bad)


# --- routes: 501 behind auth and membership -----------------------------------------------


async def test_incident_routes_need_sign_in(client: httpx.AsyncClient) -> None:
    assert (await client.post("/incidents", json=_create())).status_code == 401
    assert (await client.get(f"/incidents?workspace_id={uuid.uuid4()}")).status_code == 401
    assert (await client.get(f"/incidents/{uuid.uuid4()}")).status_code == 401


async def test_incident_routes_are_501_for_members_and_404_for_others(
    client: httpx.AsyncClient, alice: Member, bob: Member
) -> None:
    body = _create(workspace_id=str(alice.workspace_id))
    assert (await client.post("/incidents", json=body, headers=alice.headers)).status_code == 501
    assert (await client.post("/incidents", json=body, headers=bob.headers)).status_code == 404
    listing = f"/incidents?workspace_id={alice.workspace_id}&status=resolved"
    assert (await client.get(listing, headers=alice.headers)).status_code == 501
    assert (await client.get(listing, headers=bob.headers)).status_code == 404
    detail = await client.get(f"/incidents/{uuid.uuid4()}", headers=alice.headers)
    assert detail.status_code == 501


def test_incident_routes_and_payloads_are_in_openapi() -> None:
    schema = app.openapi()
    assert {"/incidents", "/incidents/{incident_id}"} <= set(schema["paths"])
    names = set(schema["components"]["schemas"])
    assert {
        "IncidentCreate",
        "IncidentCreated",
        "IncidentPage",
        "IncidentDetail",
        "IncidentOpened",
        "Diagnosis",
        "FixApproval",
        "FixApplied",
        "IncidentResolved",
        "RootCause",
        "ActionPlan",
    } <= names


# --- events -------------------------------------------------------------------------------


def test_every_incident_event_is_an_sse_event_name_and_none_clash_with_deploys() -> None:
    assert set(MEDIC_EVENT_NAMES) <= set(get_args(SseEventName))
    assert not set(MEDIC_EVENT_NAMES) & set(DEPLOY_EVENT_NAMES)


def _opened(resource_id: uuid.UUID) -> IncidentOpened:
    return IncidentOpened(
        incident_id=uuid.uuid4(),
        title="checkout-worker tasks OOM-killed",
        severity="high",
        detected_by="watcher",
        signals=[
            Signal(
                kind="ecs_tasks_stopped",
                resource_id=resource_id,
                resource_name="checkout-worker",
                detail="3 tasks stopped with exit code 137 (OutOfMemoryError)",
                at=NOW,
            )
        ],
        affected_resources=[
            ResourceRef(id=resource_id, name="checkout-worker", type="ecs:service", provider="aws")
        ],
        window_start=NOW,
    )


def _root_cause() -> RootCause:
    return RootCause(
        summary="Worker tasks are OOM-killed since commit abc123 lowered the memory limit.",
        root_cause="worker/ecs-task.json sets container_memory 24; tasks exit 137.",
        triggering_change=TriggeringChange(kind="commit", ref="abc123", at=NOW.isoformat()),
        affected_resources=["checkout-worker"],
        evidence=[
            EvidenceRef(
                source="ecs:checkout-worker",
                timestamp=NOW.isoformat(),
                line="OutOfMemoryError: Container killed due to memory usage",
            )
        ],
        confidence="high",
    )


async def test_incident_events_stream_as_named_events_with_typed_data(
    client: httpx.AsyncClient, session_factory: SessionFactory, alice: Member
) -> None:
    resource = uuid.uuid4()
    plan = ActionPlan(
        action="ecs_rollback_service",
        connection_id=CONNECTION,
        target="checkout/checkout-worker",
        params={"cluster": "checkout", "service": "checkout-worker"},
        current=f"{TASK_DEF}/checkout-worker:42",
        desired=f"{TASK_DEF}/checkout-worker:41",
        summary="Point checkout-worker back at checkout-worker:41",
        planned_at=NOW,
    )
    fix = ProposedFix(
        fix_id="fix-1",
        kind="emergency_action",
        order=1,
        title="Roll back checkout-worker",
        rationale="Production is down now.",
        action="ecs_rollback_service",
        plan=plan,
    )
    async with session_factory() as session:
        run = Run(workspace_id=alice.workspace_id, agent="medic", goal="g")
        run.status = RunStatus.succeeded
        session.add(run)
        await session.flush()
        await record_step(session, run, kind="incident_opened", data=_opened(resource))
        await record_step(session, run, kind="diagnosis", data=Diagnosis(root_cause=_root_cause()))
        await record_step(
            session,
            run,
            kind="approval_requested",
            data=FixApproval(incident_id=uuid.uuid4(), fix=fix),
        )
        await record_step(
            session,
            run,
            kind="verify_result",
            data=VerifyResult(
                ok=True,
                checks=[VerifyCheck(name="rollout", ok=True, detail="1/1 tasks running")],
                duration_ms=1200,
            ),
        )
        await session.commit()
        run_id = run.id

    resp = await client.get(await _stream_url(client, alice, run_id))
    events = _parse_sse(resp.text)
    by_name = {name: data for name, data, _ in events}
    assert by_name["incident_opened"]["data"]["signals"][0]["kind"] == "ecs_tasks_stopped"
    assert by_name["diagnosis"]["data"]["root_cause"]["triggering_change"]["kind"] == "commit"
    approval = by_name["approval_requested"]["step"]["data"]
    assert approval["type"] == "fix_approval" and approval["fix"]["plan"]["current"].endswith(":42")
    assert by_name["verify_result"]["data"]["ok"] is True

    async with session_factory() as session:
        step = await session.scalar(
            select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.seq).limit(1)
        )
        assert step is not None
        assert isinstance(StepOut.build(step).data, IncidentOpened)


# --- RootCause schema -----------------------------------------------------------------------


def test_root_cause_enums_all_allow_other() -> None:
    assert choices_without_other(RootCause) == []


def test_root_cause_needs_confidence_and_known_change_kinds() -> None:
    with pytest.raises(ValidationError):
        RootCause.model_validate({"summary": "s", "root_cause": "r"})
    with pytest.raises(ValidationError):
        TriggeringChange.model_validate({"kind": "rumour"})


def test_evidence_lines_are_one_line_each() -> None:
    line = evidence_line(NOW, "ecs", "checkout-worker", "stopped\n  exit code 137")
    assert line == f"{NOW.isoformat()} | ecs | checkout-worker | stopped exit code 137"


async def test_fake_evidence_records_what_was_asked() -> None:
    fake = FakeEvidence({"ecs:checkout-worker": "line"})
    resource = uuid.uuid4()
    assert await fake(uuid.uuid4(), [resource], NOW, NOW) == {"ecs:checkout-worker": "line"}
    assert fake.calls[0][1] == [resource]


# --- approved actions for Medic -------------------------------------------------------------


def test_medic_actions_are_approved_action_names() -> None:
    names = set(get_args(MedicAction))
    assert set(EMERGENCY_ACTION_NAMES) | {"open_fix_pr"} == names
    flat: set[str] = set()
    for part in get_args(ApprovedActionName):
        flat |= set(get_args(part))
    assert names | {"target_apply", "target_rollback"} == flat


async def test_plan_hash_ignores_wording_and_time_but_not_state(
    session_factory: SessionFactory,
) -> None:
    actions = FakeEmergencyActions(session_factory, services=checkout_services())
    first = await actions.plan_ecs_rollback_service(CONNECTION, "checkout", "checkout-worker")
    again = first.model_copy(update={"summary": "other words", "planned_at": NOW})
    moved = first.model_copy(update={"current": f"{TASK_DEF}/checkout-worker:43"})
    assert first.plan_hash() == again.plan_hash() != moved.plan_hash()


async def test_ecs_rollback_runs_once_with_its_token(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    actions = FakeEmergencyActions(session_factory, services=checkout_services())
    plan = await actions.plan_ecs_rollback_service(CONNECTION, "checkout", "checkout-worker")
    assert (
        plan.summary == "Point checkout-worker back at checkout-worker:41 (now checkout-worker:42)"
    )
    run = await medic_run(session_factory, workspace)
    token = await approve_medic_action(
        session_factory, workspace, run, action=plan.action, plan_hash=plan.plan_hash()
    )
    result = await actions.execute(plan, token, run_id=run.id)
    assert result.ok and result.details["task_definition"].endswith("checkout-worker:41")
    with pytest.raises(PolicyViolationError, match="already used"):
        await actions.execute(plan, token, run_id=run.id)


async def test_an_action_refuses_a_token_for_another_action_or_plan(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    actions = FakeEmergencyActions(
        session_factory, services=checkout_services(), secrets=checkout_secrets()
    )
    rollback = await actions.plan_ecs_rollback_service(CONNECTION, "checkout", "checkout-worker")
    redeploy = await actions.plan_force_redeploy(CONNECTION, "checkout", "checkout-worker")
    run = await medic_run(session_factory, workspace)
    token = await approve_medic_action(
        session_factory, workspace, run, action=rollback.action, plan_hash=rollback.plan_hash()
    )
    with pytest.raises(PolicyViolationError):
        await actions.execute(redeploy, token, run_id=run.id)
    with pytest.raises(PolicyViolationError):
        await actions.execute(rollback, object(), run_id=run.id)  # type: ignore[arg-type]
    assert actions.executed == []


async def test_secret_rollback_moves_stages_by_version_id_and_checks_state(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    secrets = checkout_secrets()
    actions = FakeEmergencyActions(session_factory, secrets=secrets)
    plan = await actions.plan_secret_rollback_version(CONNECTION, SECRET_ARN)
    assert (plan.current, plan.desired) == ("v-bad", "v-good")
    assert plan.summary == "Move AWSCURRENT of checkout/database-url-AbC123 back to version v-good"
    run = await medic_run(session_factory, workspace)
    token = await approve_medic_action(
        session_factory, workspace, run, action=plan.action, plan_hash=plan.plan_hash()
    )
    # Someone rotated the secret again after the plan: the approved plan no longer applies.
    secrets[SECRET_ARN].versions = {"v-good": [], "v-bad": ["AWSPREVIOUS"], "v-new": ["AWSCURRENT"]}
    result = await actions.execute(plan, token, run_id=run.id)
    assert not result.ok and "state changed" in result.summary


# --- fix PRs ----------------------------------------------------------------------------------


def _draft(**overrides: Any) -> PullRequestDraft:
    values: dict[str, Any] = {
        "installation_id": 1,
        "repo": "GroundsCrew-AI/checkout-demo",
        "base": "main",
        "branch": "groundcrew/fix-worker-memory",
        "title": "Restore the worker's memory limit",
        "body": "Reverts container_memory 24.",
        "files": {"worker/ecs-task.json": '{"cpu": "256", "memory": "512"}\n'},
        **overrides,
    }
    return PullRequestDraft(**values)


def test_fix_prs_only_go_on_groundcrew_fix_branches() -> None:
    assert check_fix_branch("groundcrew/fix-worker-memory")
    for branch in ("main", "groundcrew/fix-", "feature/x", "groundcrew/fix-../main"):
        with pytest.raises(PolicyViolationError):
            check_fix_branch(branch)


def test_pr_plan_hash_covers_file_contents() -> None:
    changed = _draft(files={"worker/ecs-task.json": "{}\n"})
    assert _draft().plan_hash() == _draft().plan_hash() != changed.plan_hash()


async def test_fake_fix_pr_needs_its_approval(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    prs = FakeFixPullRequests(session_factory)
    draft = _draft()
    run = await medic_run(session_factory, workspace)
    token = await approve_medic_action(
        session_factory, workspace, run, action="open_fix_pr", plan_hash=draft.plan_hash()
    )
    with pytest.raises(PolicyViolationError):
        await prs.open_fix_pr(_draft(title="something else"), token, run_id=run.id)
    ref = await prs.open_fix_pr(draft, token, run_id=run.id)
    assert ref.url == "https://github.com/GroundsCrew-AI/checkout-demo/pull/1"
    with pytest.raises(PolicyViolationError, match="already used"):
        await prs.open_fix_pr(draft, token, run_id=run.id)
