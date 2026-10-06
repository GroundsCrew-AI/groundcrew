"""Approve or reject a pending action. Lists live under /workspaces/{id}/approvals."""

import uuid

from fastapi import APIRouter, HTTPException, status

from app.accounts import NotFoundError, require_member
from app.db.models import Approval, Run
from app.policy.engine import ApprovalNotFoundError, ApprovalStateError, decide_approval
from app.routes.deps import CurrentUser, SessionDep
from app.routes.schemas import ApprovalOut, DecideIn

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.post("/{approval_id}/decide")
async def decide(
    approval_id: uuid.UUID, body: DecideIn, user: CurrentUser, session: SessionDep
) -> ApprovalOut:
    """Record the signed-in user's decision and resume the run."""
    approval = await session.get(Approval, approval_id)
    try:
        if approval is None:
            raise NotFoundError
        await require_member(session, user.id, approval.workspace_id)
    except NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval not found.") from None
    await session.commit()  # end the read; decide_approval locks the rows itself

    try:
        approval = await decide_approval(session, approval_id, body.decision, user.id)
    except ApprovalNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval not found.") from None
    except ApprovalStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    run = await session.get_one(Run, approval.run_id)
    return ApprovalOut.build(approval, run, user)
