"""Policy gate for tool calls (PROJECT_CONTEXT.md section 7.3).

- read tools run immediately.
- write and emergency tools NEVER run directly. They create an approvals row with the exact
  arguments, set the run to waiting_approval, and stop. Only an approved, unused approval
  for the same run, tool, and arguments lets `execute_tool` run them, exactly once.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import queue
from app.db.models import (
    TERMINAL_RUN_STATUSES,
    Approval,
    ApprovalStatus,
    Run,
    RunStatus,
    StopReason,
    ToolPolicy,
    User,
)
from app.steps import record_step
from app.tools.registry import ToolContext, ToolSpec


class PolicyViolationError(Exception):
    """A write or emergency tool was about to run without a valid approval."""


class ApprovalStateError(Exception):
    """The approval cannot be decided (already decided)."""


class ApprovalNotFoundError(ApprovalStateError):
    pass


class ApprovalForbiddenError(Exception):
    """The decider's role may not decide this approval: viewers never do, and a workspace can
    keep emergency actions for owners."""


class RunStoppedError(Exception):
    """The run was stopped (by a user) while the agent was working. The agent exits."""


class Decision(StrEnum):
    approve = "approve"
    reject = "reject"


@dataclass(frozen=True)
class Executed:
    output: str


@dataclass(frozen=True)
class AwaitingApproval:
    approval: Approval


@dataclass(frozen=True)
class Rejected:
    approval: Approval


ToolOutcome = Executed | AwaitingApproval | Rejected


def _args_json(args: BaseModel) -> dict[str, object]:
    return args.model_dump(mode="json")


async def _lock_approval(session: AsyncSession, approval: Approval) -> Approval:
    locked = await session.get(Approval, approval.id, with_for_update=True, populate_existing=True)
    if locked is None:
        raise PolicyViolationError("Approval row no longer exists.")
    return locked


async def execute_tool(
    session: AsyncSession,
    ctx: ToolContext,
    spec: ToolSpec,
    args: BaseModel,
    *,
    approval: Approval | None = None,
) -> str:
    """The only place a tool function is called."""
    if spec.policy != ToolPolicy.read:
        if approval is None:
            raise PolicyViolationError(f"{spec.policy} tool {spec.name!r} needs an approval.")
        approval = await _lock_approval(session, approval)
        if approval.status != ApprovalStatus.approved:
            raise PolicyViolationError(
                f"Approval {approval.id} is {approval.status}, not approved."
            )
        if approval.executed_at is not None:
            raise PolicyViolationError(f"Approval {approval.id} was already used.")
        if approval.run_id != ctx.run_id or approval.tool_name != spec.name:
            raise PolicyViolationError(f"Approval {approval.id} is for a different run or tool.")
        if approval.arguments != _args_json(args):
            raise PolicyViolationError(f"Approval {approval.id} was for different arguments.")
        # Mark used and commit before running: a write runs at most once, even after a crash.
        approval.executed_at = datetime.now(UTC)
        await session.commit()
    return await spec.fn(ctx, args)


async def handle_tool_call(
    session: AsyncSession,
    run: Run,
    spec: ToolSpec,
    args: BaseModel,
    tool_call_id: str,
    *,
    repo_root: Path | None = None,
) -> ToolOutcome:
    ctx = ToolContext(run_id=run.id, workspace_id=run.workspace_id, repo_root=repo_root)
    if spec.policy == ToolPolicy.read:
        return Executed(await execute_tool(session, ctx, spec, args))

    approval = await session.scalar(
        select(Approval).where(Approval.run_id == run.id, Approval.tool_call_id == tool_call_id)
    )
    if approval is None:
        approval = Approval(
            run_id=run.id,
            workspace_id=run.workspace_id,
            tool_call_id=tool_call_id,
            tool_name=spec.name,
            policy=spec.policy,
            arguments=_args_json(args),
            status=ApprovalStatus.pending,
        )
        session.add(approval)
        await session.flush()
        await record_step(
            session,
            run,
            kind="approval_requested",
            tool_name=spec.name,
            input_summary=json.dumps(approval.arguments),
            output_summary=f"Waiting for approval {approval.id} ({spec.policy} action).",
            approval_id=approval.id,
        )

    if approval.status == ApprovalStatus.pending:
        # Lock the run: a user may have stopped it while the model was thinking.
        locked = await session.get(Run, run.id, with_for_update=True, populate_existing=True)
        if locked is None or locked.status in TERMINAL_RUN_STATUSES:
            await session.rollback()
            raise RunStoppedError(str(run.id))
        run.status = RunStatus.waiting_approval
        run.stop_reason = StopReason.waiting_approval
        await session.commit()
        return AwaitingApproval(approval)
    if approval.status == ApprovalStatus.rejected:
        return Rejected(approval)
    if approval.executed_at is not None:
        # The worker stopped after starting this action and before saving its result.
        # Never run it again; tell the model the outcome is unknown.
        return Executed(
            f"error: this approved {spec.name} action was started at "
            f"{approval.executed_at.isoformat()} but its result was not saved (the worker "
            "stopped). It may or may not have completed. Do not retry it. Check the current "
            "state with a read tool and report what you find."
        )
    # Run exactly what was approved, not whatever the conversation says now.
    approved_args = spec.args_model.model_validate(approval.arguments)
    return Executed(await execute_tool(session, ctx, spec, approved_args, approval=approval))


async def _waiting_for(session: AsyncSession, run: Run, approval: Approval) -> str | None:
    """For a Medic fix with `depends_on`: why it cannot be approved yet (its prerequisite is not
    applied), or None. Rejecting is always allowed."""
    if run.incident_id is None:
        return None
    # Imported here: app.agents.medic imports the policy package.
    from app.agents.medic.state import MedicState
    from app.db.models import Incident

    incident = await session.get(Incident, run.incident_id)
    if incident is None or not incident.state:
        return None
    fixes = MedicState.model_validate(incident.state).fixes
    by_id = {r.fix.fix_id: r for r in fixes}
    rec = next((r for r in fixes if r.approval_id == approval.id), None)
    if rec is None:
        return None
    for dep in rec.fix.depends_on:
        first = by_id.get(dep)
        if first is not None and first.status != "applied":
            return f"Fix {first.fix.order} ({first.fix.title}) must be applied before this one."
    return None


async def _check_approver(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """Only owners and approvers decide approvals (routes check it too)."""
    from app.accounts import member_role, role_allows
    from app.db.models import WorkspaceRole

    role = await member_role(session, user_id, workspace_id)
    if role is None or not role_allows(role, WorkspaceRole.approver):
        raise ApprovalForbiddenError("Only owners and approvers can approve or reject actions.")


async def _check_emergency_approver(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    from app.accounts import member_role
    from app.db.models import Workspace, WorkspaceRole
    from app.workspace_settings import get_config

    workspace = await session.get_one(Workspace, workspace_id)
    if get_config(workspace).emergency_approvers == "approvers":
        return
    if await member_role(session, user_id, workspace_id) != WorkspaceRole.owner:
        raise ApprovalForbiddenError(
            "Only a workspace owner can approve emergency actions here (workspace settings)."
        )


async def decide_approval(
    session: AsyncSession,
    approval_id: uuid.UUID,
    decision: Decision,
    decided_by: uuid.UUID | None,
    *,
    via: str | None = None,
) -> Approval:
    """Record a decision by user `decided_by` (or, with no user, `via`: the dev self-approve
    path) and enqueue a job that resumes the run. Commits."""
    if decided_by is None and not via:
        raise ApprovalStateError("A decision needs a user or a recorded source.")
    approval = await session.get(Approval, approval_id, with_for_update=True)
    if approval is None:
        raise ApprovalNotFoundError("Approval not found.")
    if approval.status != ApprovalStatus.pending:
        raise ApprovalStateError(f"Approval is already {approval.status}.")
    run = await session.get_one(Run, approval.run_id, with_for_update=True)
    if run.status in TERMINAL_RUN_STATUSES:
        raise ApprovalStateError(f"The run is already {run.status}.")
    # Owners and approvers decide; rejecting needs nothing more, approving an emergency action
    # may need an owner. (No decided_by: the dev-only self-approve for live scripts.)
    if decided_by is not None:
        await _check_approver(session, approval.workspace_id, decided_by)
    if (
        approval.policy == ToolPolicy.emergency
        and decision == Decision.approve
        and decided_by is not None
    ):
        await _check_emergency_approver(session, approval.workspace_id, decided_by)
    if decision == Decision.approve:
        waiting = await _waiting_for(session, run, approval)
        if waiting is not None:
            raise ApprovalStateError(waiting)
    decider = await session.get(User, decided_by) if decided_by is not None else None
    label = via or ((decider.github_login or decider.name) if decider else None)

    approval.status = (
        ApprovalStatus.approved if decision == Decision.approve else ApprovalStatus.rejected
    )
    approval.decided_by = decided_by
    approval.decided_via = via
    approval.decided_at = datetime.now(UTC)

    await record_step(
        session,
        run,
        kind="approval_decided",
        tool_name=approval.tool_name,
        output_summary=f"{approval.status} by {label or decided_by}",
        approval_id=approval.id,
    )
    if run.status == RunStatus.waiting_approval:
        run.status = RunStatus.queued
        run.stop_reason = None
        await queue.enqueue(
            session, "resume_run", {"run_id": str(run.id), "approval_id": str(approval.id)}
        )
    await session.commit()
    return approval
