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

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import queue
from app.db.models import Approval, ApprovalStatus, Run, RunStatus, StopReason, ToolPolicy
from app.steps import record_step
from app.tools.registry import ToolContext, ToolSpec


class PolicyViolationError(Exception):
    """A write or emergency tool was about to run without a valid approval."""


class ApprovalStateError(Exception):
    """The approval cannot be decided (already decided)."""


class ApprovalNotFoundError(ApprovalStateError):
    pass


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
) -> ToolOutcome:
    ctx = ToolContext(run_id=run.id, workspace_id=run.workspace_id)
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
        )

    if approval.status == ApprovalStatus.pending:
        run.status = RunStatus.waiting_approval
        run.stop_reason = StopReason.waiting_approval
        await session.commit()
        return AwaitingApproval(approval)
    if approval.status == ApprovalStatus.rejected:
        return Rejected(approval)
    # Run exactly what was approved, not whatever the conversation says now.
    approved_args = spec.args_model.model_validate(approval.arguments)
    return Executed(await execute_tool(session, ctx, spec, approved_args, approval=approval))


async def decide_approval(
    session: AsyncSession,
    approval_id: uuid.UUID,
    decision: Decision,
    decided_by: str,
) -> Approval:
    """Record a decision and enqueue a job that resumes the run. Commits."""
    approval = await session.get(Approval, approval_id, with_for_update=True)
    if approval is None:
        raise ApprovalNotFoundError("Approval not found.")
    if approval.status != ApprovalStatus.pending:
        raise ApprovalStateError(f"Approval is already {approval.status}.")

    approval.status = (
        ApprovalStatus.approved if decision == Decision.approve else ApprovalStatus.rejected
    )
    approval.decided_by = decided_by
    approval.decided_at = datetime.now(UTC)

    run = await session.get_one(Run, approval.run_id, with_for_update=True)
    await record_step(
        session,
        run,
        kind="approval_decided",
        tool_name=approval.tool_name,
        output_summary=f"{approval.status} by {decided_by}",
    )
    if run.status == RunStatus.waiting_approval:
        run.status = RunStatus.queued
        run.stop_reason = None
        await queue.enqueue(
            session, "resume_run", {"run_id": str(run.id), "approval_id": str(approval.id)}
        )
    await session.commit()
    return approval
