"""ApprovedAction tokens: the only way into Target.apply() and Target.rollback().

Signed with APPROVAL_TOKEN_KEY (valid across workers and restarts) and spent once in the
database (approvals.used_at, one atomic UPDATE)."""

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.db.models import Approval, ApprovalStatus, ToolPolicy, Workspace
from app.db.session import SessionFactory
from app.policy import actions
from app.policy.actions import (
    TARGET_APPLY,
    TARGET_ROLLBACK,
    ApprovalKeyError,
    ApprovedAction,
    consume_approved_action,
    issue_approved_action,
)
from app.policy.engine import PolicyViolationError

from .conftest import TEST_DATABASE_URL
from .target_fakes import approved_action

pytestmark = pytest.mark.anyio

PLAN = "a" * 64
OTHER_PLAN = "b" * 64


async def _approved(
    sf: SessionFactory, ws: Workspace, **kw: object
) -> tuple[uuid.UUID, ApprovedAction]:
    run, _, token = await approved_action(sf, ws, action=TARGET_APPLY, plan_hash=PLAN, **kw)  # type: ignore[arg-type]
    assert token is not None
    return run.id, token


async def _approve(sf: SessionFactory, approval: Approval) -> Approval:
    async with sf() as session:
        stored = await session.get(Approval, approval.id)
        assert stored is not None
        stored.status = ApprovalStatus.approved
        await session.commit()
        return stored


async def test_a_token_works_once(session_factory: SessionFactory, workspace: Workspace) -> None:
    run_id, token = await _approved(session_factory, workspace)
    await consume_approved_action(
        session_factory, token, run_id=run_id, action=TARGET_APPLY, plan_hash=PLAN
    )
    with pytest.raises(PolicyViolationError, match="already used"):
        await consume_approved_action(
            session_factory, token, run_id=run_id, action=TARGET_APPLY, plan_hash=PLAN
        )
    async with session_factory() as session:
        stored = await session.get(Approval, token.approval_id)
        assert stored is not None and stored.used_at is not None and stored.executed_at is not None
        with pytest.raises(PolicyViolationError, match="already used"):
            await issue_approved_action(
                session, stored, run_id=run_id, action=TARGET_APPLY, plan_hash=PLAN
            )


async def test_two_tokens_from_one_approval_still_run_once(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    run_id, first = await _approved(session_factory, workspace)
    async with session_factory() as session:
        approval = await session.get(Approval, first.approval_id)
        assert approval is not None
        second = await issue_approved_action(
            session, approval, run_id=run_id, action=TARGET_APPLY, plan_hash=PLAN
        )
    await consume_approved_action(
        session_factory, second, run_id=run_id, action=TARGET_APPLY, plan_hash=PLAN
    )
    with pytest.raises(PolicyViolationError, match="already used"):
        await consume_approved_action(
            session_factory, first, run_id=run_id, action=TARGET_APPLY, plan_hash=PLAN
        )


# Runs in a new Python process: nothing in memory from the process that issued the token.
_OTHER_WORKER = """
import asyncio, os, sys, uuid
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.policy.actions import ApprovedAction, consume_approved_action

async def main() -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    token = ApprovedAction.decode(sys.argv[1])
    await consume_approved_action(
        async_sessionmaker(engine), token, run_id=uuid.UUID(sys.argv[2]),
        action="target_apply", plan_hash=sys.argv[3],
    )
    await engine.dispose()
    print("consumed")

asyncio.run(main())
"""


async def test_a_token_survives_a_worker_restart(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    run_id, token = await _approved(session_factory, workspace)
    carried = token.encode()  # what a job payload would hold

    def other_worker() -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            [sys.executable, "-c", _OTHER_WORKER, carried, str(run_id), PLAN],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
            timeout=60,
            check=False,
        )

    first = await asyncio.to_thread(other_worker)
    assert first.returncode == 0 and first.stdout.strip() == "consumed", first.stderr[-2000:]
    again = await asyncio.to_thread(other_worker)
    assert again.returncode != 0 and "already used" in again.stderr


async def test_two_workers_racing_for_one_token_only_one_wins(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    run_id, token = await _approved(session_factory, workspace)
    carried = token.encode()

    async def worker() -> str:
        try:
            await consume_approved_action(
                session_factory,
                ApprovedAction.decode(carried),
                run_id=run_id,
                action=TARGET_APPLY,
                plan_hash=PLAN,
            )
            return "ran"
        except PolicyViolationError:
            return "refused"

    results = await asyncio.gather(*(worker() for _ in range(8)))
    assert sorted(results) == ["ran"] + ["refused"] * 7


async def test_a_token_signed_with_another_key_is_refused(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    run_id, token = await _approved(session_factory, workspace)
    other = Settings(approval_token_key=SecretStr("another-key-entirely-0123456789"))
    with pytest.raises(PolicyViolationError, match="not issued by the policy engine"):
        await consume_approved_action(
            session_factory,
            token,
            run_id=run_id,
            action=TARGET_APPLY,
            plan_hash=PLAN,
            settings=other,
        )


def test_the_key_is_required_outside_dev() -> None:
    with pytest.raises(ApprovalKeyError, match="APPROVAL_TOKEN_KEY"):
        actions._key(
            Settings(app_env="production", approval_token_key=None, secrets_key=SecretStr("k"))
        )
    dev = Settings(app_env="dev", approval_token_key=None, secrets_key=SecretStr("fernet-key"))
    assert actions._key(dev) != b"fernet-key" and len(actions._key(dev)) == 32


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


async def test_a_token_stops_working_if_the_approval_is_withdrawn(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    run_id, token = await _approved(session_factory, workspace)
    async with session_factory() as session:
        stored = await session.get(Approval, token.approval_id)
        assert stored is not None
        stored.status = ApprovalStatus.rejected
        await session.commit()
    with pytest.raises(PolicyViolationError, match="no longer approved"):
        await consume_approved_action(
            session_factory, token, run_id=run_id, action=TARGET_APPLY, plan_hash=PLAN
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
    stored = await _approve(session_factory, approval)
    async with session_factory() as session:
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
    stored = await _approve(session_factory, approval)
    async with session_factory() as session:
        with pytest.raises(PolicyViolationError, match="write target_rollback"):
            await issue_approved_action(
                session, stored, run_id=run.id, action=TARGET_ROLLBACK, plan_hash=PLAN
            )


async def test_a_token_is_bound_to_its_plan_run_and_action(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    run_id, token = await _approved(session_factory, workspace)
    with pytest.raises(PolicyViolationError, match="different plan"):
        await consume_approved_action(
            session_factory, token, run_id=run_id, action=TARGET_APPLY, plan_hash=OTHER_PLAN
        )
    with pytest.raises(PolicyViolationError, match="different run"):
        await consume_approved_action(
            session_factory, token, run_id=uuid.uuid4(), action=TARGET_APPLY, plan_hash=PLAN
        )
    with pytest.raises(PolicyViolationError, match="is for target_apply"):
        await consume_approved_action(
            session_factory, token, run_id=run_id, action=TARGET_ROLLBACK, plan_hash=PLAN
        )
    # The refused attempts did not spend it.
    await consume_approved_action(
        session_factory, token, run_id=run_id, action=TARGET_APPLY, plan_hash=PLAN
    )


async def test_a_hand_made_or_altered_token_is_refused(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    run_id, token = await _approved(session_factory, workspace)
    forged = token.model_copy(update={"approval_id": uuid.uuid4(), "seal": "0" * 64})
    with pytest.raises(PolicyViolationError, match="not issued by the policy engine"):
        await consume_approved_action(
            session_factory, forged, run_id=run_id, action=TARGET_APPLY, plan_hash=PLAN
        )
    retargeted = token.model_copy(update={"plan_hash": OTHER_PLAN})
    with pytest.raises(PolicyViolationError, match="not issued by the policy engine"):
        await consume_approved_action(
            session_factory, retargeted, run_id=run_id, action=TARGET_APPLY, plan_hash=OTHER_PLAN
        )
    with pytest.raises(PolicyViolationError, match="needs an approved action"):
        await consume_approved_action(
            session_factory, None, run_id=run_id, action=TARGET_APPLY, plan_hash=PLAN
        )
    with pytest.raises(PolicyViolationError, match="Not an approved action token"):
        ApprovedAction.decode("not-a-token")
