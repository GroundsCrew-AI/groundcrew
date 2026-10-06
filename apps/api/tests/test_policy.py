"""The policy gate: write and emergency tools never run without an approved approval."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.db.models import (
    Approval,
    ApprovalStatus,
    Job,
    Run,
    RunStatus,
    StopReason,
    ToolPolicy,
    Workspace,
)
from app.db.session import SessionFactory
from app.policy.engine import (
    ApprovalStateError,
    AwaitingApproval,
    Decision,
    Executed,
    PolicyViolationError,
    Rejected,
    decide_approval,
    execute_tool,
    handle_tool_call,
)
from app.tools.registry import ToolContext, ToolRegistry, ToolSpec

pytestmark = pytest.mark.anyio


class Target(BaseModel):
    name: str


def _registry(calls: list[str]) -> ToolRegistry:
    reg = ToolRegistry()

    @reg.tool(name="look", description="read", args=Target, policy=ToolPolicy.read)
    async def look(_ctx: ToolContext, args: Target) -> str:
        calls.append(f"look {args.name}")
        return f"looked at {args.name}"

    @reg.tool(name="restart", description="write", args=Target, policy=ToolPolicy.write)
    async def restart(_ctx: ToolContext, args: Target) -> str:
        calls.append(f"restart {args.name}")
        return f"restarted {args.name}"

    @reg.tool(name="rollback", description="emergency", args=Target, policy=ToolPolicy.emergency)
    async def rollback(_ctx: ToolContext, args: Target) -> str:
        calls.append(f"rollback {args.name}")
        return f"rolled back {args.name}"

    return reg


async def _run(sf: SessionFactory, ws: Workspace) -> Run:
    async with sf() as session:
        run = Run(workspace_id=ws.id, agent="test", goal="g", status=RunStatus.running)
        session.add(run)
        await session.commit()
        return run


async def _approval(
    sf: SessionFactory, run: Run, tool: str, args: dict[str, Any], status: ApprovalStatus
) -> Approval:
    async with sf() as session:
        approval = Approval(
            run_id=run.id,
            workspace_id=run.workspace_id,
            tool_call_id=f"call-{tool}-{status}",
            tool_name=tool,
            policy=ToolPolicy.write,
            arguments=args,
            status=status,
        )
        session.add(approval)
        await session.commit()
        return approval


def test_policy_tag_is_required() -> None:
    reg = ToolRegistry()
    with pytest.raises(TypeError):
        reg.tool(name="x", description="x", args=Target, policy="write")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        reg.tool(name="x", description="x", args=Target)  # type: ignore[call-arg]


def test_openai_schema_from_pydantic() -> None:
    spec = _registry([]).get("restart")
    schema = spec.openai_schema()
    assert schema["function"]["name"] == "restart"
    assert schema["function"]["parameters"]["properties"]["name"]["type"] == "string"


async def test_read_tool_runs_immediately(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    calls: list[str] = []
    run = await _run(session_factory, workspace)
    async with session_factory() as session:
        outcome = await handle_tool_call(
            session, run, _registry(calls).get("look"), Target(name="api"), "c1"
        )
    assert outcome == Executed("looked at api")
    assert calls == ["look api"]


@pytest.mark.parametrize("tool", ["restart", "rollback"])
async def test_write_and_emergency_create_approval_and_do_not_run(
    session_factory: SessionFactory, workspace: Workspace, tool: str
) -> None:
    calls: list[str] = []
    run = await _run(session_factory, workspace)
    async with session_factory() as session:
        attached = await session.get_one(Run, run.id)
        outcome = await handle_tool_call(
            session, attached, _registry(calls).get(tool), Target(name="api"), "c1"
        )
    assert isinstance(outcome, AwaitingApproval)
    assert calls == []
    async with session_factory() as session:
        approval = await session.scalar(select(Approval).where(Approval.run_id == run.id))
        stored_run = await session.get_one(Run, run.id)
    assert approval is not None
    assert approval.status == ApprovalStatus.pending
    assert approval.tool_name == tool
    assert approval.arguments == {"name": "api"}
    assert stored_run.status == RunStatus.waiting_approval
    assert stored_run.stop_reason == StopReason.waiting_approval


@pytest.mark.parametrize("tool", ["restart", "rollback"])
async def test_execute_tool_refuses_without_approval(
    session_factory: SessionFactory, workspace: Workspace, tool: str
) -> None:
    calls: list[str] = []
    spec: ToolSpec = _registry(calls).get(tool)
    run = await _run(session_factory, workspace)
    ctx = ToolContext(run_id=run.id, workspace_id=run.workspace_id)
    async with session_factory() as session:
        with pytest.raises(PolicyViolationError):
            await execute_tool(session, ctx, spec, Target(name="api"))
    assert calls == []


@pytest.mark.parametrize(
    ("status", "tool", "args"),
    [
        (ApprovalStatus.pending, "restart", {"name": "api"}),
        (ApprovalStatus.rejected, "restart", {"name": "api"}),
        (ApprovalStatus.approved, "restart", {"name": "web"}),  # different arguments
        (ApprovalStatus.approved, "rollback", {"name": "api"}),  # different tool
    ],
)
async def test_execute_tool_refuses_invalid_approval(
    session_factory: SessionFactory,
    workspace: Workspace,
    status: ApprovalStatus,
    tool: str,
    args: dict[str, Any],
) -> None:
    calls: list[str] = []
    run = await _run(session_factory, workspace)
    approval = await _approval(session_factory, run, tool, args, status)
    ctx = ToolContext(run_id=run.id, workspace_id=run.workspace_id)
    async with session_factory() as session:
        with pytest.raises(PolicyViolationError):
            await execute_tool(
                session, ctx, _registry(calls).get("restart"), Target(name="api"), approval=approval
            )
    assert calls == []


async def test_approval_from_another_run_is_refused(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    calls: list[str] = []
    other_run = await _run(session_factory, workspace)
    run = await _run(session_factory, workspace)
    approval = await _approval(
        session_factory, other_run, "restart", {"name": "api"}, ApprovalStatus.approved
    )
    ctx = ToolContext(run_id=run.id, workspace_id=run.workspace_id)
    async with session_factory() as session:
        with pytest.raises(PolicyViolationError):
            await execute_tool(
                session, ctx, _registry(calls).get("restart"), Target(name="api"), approval=approval
            )
    assert calls == []


async def test_approved_runs_exactly_once(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    calls: list[str] = []
    spec = _registry(calls).get("restart")
    run = await _run(session_factory, workspace)
    approval = await _approval(
        session_factory, run, "restart", {"name": "api"}, ApprovalStatus.approved
    )
    ctx = ToolContext(run_id=run.id, workspace_id=run.workspace_id)
    async with session_factory() as session:
        assert await execute_tool(session, ctx, spec, Target(name="api"), approval=approval) == (
            "restarted api"
        )
    async with session_factory() as session:
        stored = await session.get_one(Approval, approval.id)
        assert stored.executed_at is not None and stored.executed_at <= datetime.now(UTC)
        with pytest.raises(PolicyViolationError, match="already used"):
            await execute_tool(session, ctx, spec, Target(name="api"), approval=stored)
    assert calls == ["restart api"]


async def test_decide_enqueues_resume_and_handle_uses_decision(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    calls: list[str] = []
    reg = _registry(calls)
    run = await _run(session_factory, workspace)
    async with session_factory() as session:
        stored_run = await session.get_one(Run, run.id)
        first = await handle_tool_call(
            session, stored_run, reg.get("restart"), Target(name="api"), "c1"
        )
        second = await handle_tool_call(
            session, stored_run, reg.get("restart"), Target(name="web"), "c2"
        )
    assert isinstance(first, AwaitingApproval) and isinstance(second, AwaitingApproval)

    async with session_factory() as session:
        await decide_approval(session, first.approval.id, Decision.approve, workspace.owner_id)
    async with session_factory() as session:
        jobs = (await session.scalars(select(Job))).all()
        stored_run = await session.get_one(Run, run.id)
    assert [j.kind for j in jobs] == ["resume_run"]
    assert jobs[0].payload == {"run_id": str(run.id), "approval_id": str(first.approval.id)}
    assert stored_run.status == RunStatus.queued and stored_run.stop_reason is None

    async with session_factory() as session:
        with pytest.raises(ApprovalStateError, match="already approved"):
            await decide_approval(session, first.approval.id, Decision.reject, workspace.owner_id)
        await decide_approval(session, second.approval.id, Decision.reject, workspace.owner_id)

    async with session_factory() as session:
        stored_run = await session.get_one(Run, run.id)
        approved = await handle_tool_call(
            session, stored_run, reg.get("restart"), Target(name="ignored"), "c1"
        )
        rejected = await handle_tool_call(
            session, stored_run, reg.get("restart"), Target(name="web"), "c2"
        )
    # The approved arguments run, not the ones passed now.
    assert approved == Executed("restarted api")
    assert isinstance(rejected, Rejected)
    assert calls == ["restart api"]
