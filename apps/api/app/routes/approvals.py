"""Approve or reject a pending action. Lists live under /workspaces/{id}/approvals."""

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.accounts import NotFoundError, require_member
from app.audit import record
from app.db.models import Approval, Run, WorkspaceRole
from app.policy.engine import (
    ApprovalForbiddenError,
    ApprovalNotFoundError,
    ApprovalStateError,
    PolicyViolationError,
    decide_approval,
)
from app.policy.self_approve import self_approve, self_approve_allowed
from app.routes.deps import ClientIp, CurrentUser, SessionDep, SettingsDep
from app.routes.schemas import ApprovalOut, DecideIn

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.post("/{approval_id}/decide")
async def decide(
    approval_id: uuid.UUID, body: DecideIn, user: CurrentUser, session: SessionDep, ip: ClientIp
) -> ApprovalOut:
    """Record the signed-in user's decision and resume the run."""
    approval = await session.get(Approval, approval_id)
    try:
        if approval is None:
            raise NotFoundError
        # Approvals are for owners and approvers; viewers get a 403.
        await require_member(session, user.id, approval.workspace_id, WorkspaceRole.approver)
    except NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval not found.") from None
    await session.commit()  # end the read; decide_approval locks the rows itself

    try:
        approval = await decide_approval(session, approval_id, body.decision, user.id)
    except ApprovalNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval not found.") from None
    except ApprovalStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except ApprovalForbiddenError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None
    run = await session.get_one(Run, approval.run_id)
    record(
        session,
        workspace_id=approval.workspace_id,
        actor=user,
        action=f"approval.{approval.status}",
        target_type="approval",
        target_id=approval.id,
        detail={
            "tool": approval.tool_name,
            "policy": str(approval.policy),
            "run_id": str(run.id),
            "agent": run.agent,
        },
        ip=ip,
    )
    await session.commit()
    return ApprovalOut.build(approval, run, user)


class SelfApproveIn(BaseModel):
    # Recorded as "script:<name> (dev)".
    script: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$", examples=["live-phase4"])


@router.post("/{approval_id}/self-approve", include_in_schema=False)
async def self_approve_route(
    approval_id: uuid.UUID,
    body: SelfApproveIn,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> ApprovalOut:
    """Dev only (APP_ENV=dev and LIVE_SCRIPT_SELF_APPROVE=1): a live test script approves the
    deploy it started. 404 everywhere else, as if the route did not exist."""
    if not self_approve_allowed(settings):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    approval = await session.get(Approval, approval_id)
    try:
        if approval is None:
            raise NotFoundError
        await require_member(session, user.id, approval.workspace_id)
    except NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval not found.") from None
    await session.commit()
    try:
        approval = await self_approve(session, approval_id, script=body.script, settings=settings)
    except ApprovalNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval not found.") from None
    except (ApprovalStateError, PolicyViolationError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    run = await session.get_one(Run, approval.run_id)
    return ApprovalOut.build(approval, run, None)
