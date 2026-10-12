"""ApprovedAction tokens: the only way into Target.apply() and Target.rollback()."""

import dataclasses
import uuid

import pytest

from app.db.models import Approval, ApprovalStatus, ToolPolicy, Workspace
from app.db.session import SessionFactory
from app.policy.actions import (
    TARGET_APPLY,
    TARGET_ROLLBACK,
    ApprovedAction,
    consume_approved_action,
    issue_approved_action,
)
from app.policy.engine import PolicyViolationError

from .target_fakes import approved_action

pytestmark = pytest.mark.anyio

PLAN = "a" * 64
OTHER_PLAN = "b" * 64


async def test_a_token_works_once(session_factory: SessionFactory, workspace: Workspace) -> None:
    run, _, token = await approved_action(
        session_factory, workspace, action=TARGET_APPLY, plan_hash=PLAN
    )
    assert token is not None
    consume_approved_action(token, run_id=run.id, action=TARGET_APPLY, plan_hash=PLAN)
    with pytest.raises(PolicyViolationError, match="already used"):
        consume_approved_action(token, run_id=run.id, action=TARGET_APPLY, plan_hash=PLAN)


async def test_an_approval_gives_one_token(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    run, approval, _ = await approved_action(
        session_factory, workspace, action=TARGET_APPLY, plan_hash=PLAN
    )
    async with session_factory() as session:
        stored = await session.get(Approval, approval.id)
        assert stored is not None and stored.executed_at is not None
        with pytest.raises(PolicyViolationError, match="already used"):
            await issue_approved_action(
                session, stored, run_id=run.id, action=TARGET_APPLY, plan_hash=PLAN
            )


@pytest.mark.parametrize("status", [ApprovalStatus.pending, ApprovalStatus.rejected])
async def test_no_token_without_an_approved_approval(
    session_factory: SessionFactory, workspace: Workspace, status: ApprovalStatus
) -> None:
    run, approval, _ = await approved_action(
        session_factory, workspace, action=TARGET_APPLY, plan_hash=PLAN, status=status
    )
    async with session_factory() as session:
        with pytest.raises(PolicyViolationError, match="not approved"):
            await issue_approved_action(
                session, approval, run_id=run.id, action=TARGET_APPLY, plan_hash=PLAN
            )


async def test_no_token_for_another_plan_run_or_action(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    run, approval, _ = await approved_action(
        session_factory,
        workspace,
        action=TARGET_APPLY,
        plan_hash=PLAN,
        status=ApprovalStatus.pending,
    )
    async with session_factory() as session:
        stored = await session.get(Approval, approval.id)
        assert stored is not None
        stored.status = ApprovalStatus.approved
        await session.commit()
        with pytest.raises(PolicyViolationError, match="different plan"):
            await issue_approved_action(
                session, stored, run_id=run.id, action=TARGET_APPLY, plan_hash=OTHER_PLAN
            )
        with pytest.raises(PolicyViolationError, match="different run"):
            await issue_approved_action(
                session, stored, run_id=uuid.uuid4(), action=TARGET_APPLY, plan_hash=PLAN
            )
        with pytest.raises(PolicyViolationError, match="not target_rollback"):
            await issue_approved_action(
                session, stored, run_id=run.id, action=TARGET_ROLLBACK, plan_hash=PLAN
            )


async def test_a_rollback_needs_an_emergency_approval(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    run, approval, _ = await approved_action(
        session_factory,
        workspace,
        action=TARGET_ROLLBACK,
        plan_hash=PLAN,
        policy=ToolPolicy.write,
        status=ApprovalStatus.pending,
    )
    async with session_factory() as session:
        stored = await session.get(Approval, approval.id)
        assert stored is not None
        stored.status = ApprovalStatus.approved
        await session.commit()
        with pytest.raises(PolicyViolationError, match="write target_rollback"):
            await issue_approved_action(
                session, stored, run_id=run.id, action=TARGET_ROLLBACK, plan_hash=PLAN
            )


async def test_a_token_is_bound_to_its_plan_run_and_action(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    run, _, token = await approved_action(
        session_factory, workspace, action=TARGET_APPLY, plan_hash=PLAN
    )
    assert token is not None
    with pytest.raises(PolicyViolationError, match="different plan"):
        consume_approved_action(token, run_id=run.id, action=TARGET_APPLY, plan_hash=OTHER_PLAN)
    with pytest.raises(PolicyViolationError, match="different run"):
        consume_approved_action(token, run_id=uuid.uuid4(), action=TARGET_APPLY, plan_hash=PLAN)
    with pytest.raises(PolicyViolationError, match="is for target_apply"):
        consume_approved_action(token, run_id=run.id, action=TARGET_ROLLBACK, plan_hash=PLAN)
    # The failed attempts did not spend it.
    consume_approved_action(token, run_id=run.id, action=TARGET_APPLY, plan_hash=PLAN)


async def test_a_hand_made_token_is_refused(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    run, _, token = await approved_action(
        session_factory, workspace, action=TARGET_APPLY, plan_hash=PLAN
    )
    assert token is not None
    forged = ApprovedAction(
        approval_id=uuid.uuid4(), run_id=run.id, action=TARGET_APPLY, plan_hash=PLAN, seal="0" * 64
    )
    with pytest.raises(PolicyViolationError, match="not issued by the policy engine"):
        consume_approved_action(forged, run_id=run.id, action=TARGET_APPLY, plan_hash=PLAN)
    retargeted = dataclasses.replace(token, plan_hash=OTHER_PLAN)
    with pytest.raises(PolicyViolationError, match="not issued by the policy engine"):
        consume_approved_action(
            retargeted, run_id=run.id, action=TARGET_APPLY, plan_hash=OTHER_PLAN
        )
    with pytest.raises(PolicyViolationError, match="needs an approved action"):
        consume_approved_action(None, run_id=run.id, action=TARGET_APPLY, plan_hash=PLAN)
