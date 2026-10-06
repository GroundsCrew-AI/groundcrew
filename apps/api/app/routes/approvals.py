"""Approvals inbox: list approvals and approve or reject one."""

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.db.models import Approval, ApprovalStatus
from app.policy.engine import ApprovalNotFoundError, ApprovalStateError, decide_approval
from app.routes.deps import SessionDep
from app.routes.schemas import ApprovalOut, DecideIn

router = APIRouter(prefix="/approvals", tags=["approvals"])

# TODO Phase 2: the logged-in user's ID instead of a fixed label.
DEV_DECIDER = "dev (no auth yet)"


@router.get("")
async def list_approvals(
    session: SessionDep, status: ApprovalStatus | None = None, limit: int = 100
) -> list[ApprovalOut]:
    query = select(Approval).order_by(Approval.created_at.desc()).limit(min(limit, 500))
    if status is not None:
        query = query.where(Approval.status == status)
    return [ApprovalOut.model_validate(a) for a in await session.scalars(query)]


@router.post("/{approval_id}/decide")
async def decide(approval_id: uuid.UUID, body: DecideIn, session: SessionDep) -> ApprovalOut:
    try:
        approval = await decide_approval(session, approval_id, body.decision, DEV_DECIDER)
    except ApprovalNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    except ApprovalStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return ApprovalOut.model_validate(approval)
