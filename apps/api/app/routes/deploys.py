"""Deploys: Scout -> Builder -> Deployer in one run (docs/deploys-contract.md).

The contract is final; the pipeline behind it lands in Milestone 6. Until then every route
authenticates, checks workspace membership (404 for non-members, like everywhere else), and
answers 501.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status

from app.accounts import NotFoundError, member_workspace
from app.routes.deploy_schemas import DeployCreate, DeployCreated, DeployDetail, DeployPage
from app.routes.deps import CurrentUser, SessionDep

router = APIRouter(prefix="/deploys", tags=["deploys"])

_NOT_IMPLEMENTED: dict[int | str, dict[str, Any]] = {
    501: {"description": "The deploy pipeline is not wired up yet."}
}


def _not_implemented() -> HTTPException:
    return HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "Deploys are not available yet.")


async def _check_member(session: SessionDep, user: CurrentUser, workspace_id: uuid.UUID) -> None:
    try:
        await member_workspace(session, user, workspace_id)
    except NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found.") from None


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        409: {"description": "Target is AWS observe tier, or the source is not GitHub."},
        **_NOT_IMPLEMENTED,
    },
)
async def create_deploy(
    body: DeployCreate, user: CurrentUser, session: SessionDep
) -> DeployCreated:
    """Start a deploy run. Returns at once; follow the run's event stream for progress.

    Nothing touches infrastructure before a workspace member approves the rendered plan.
    """
    await _check_member(session, user, body.workspace_id)
    raise _not_implemented()


@router.get("", responses=_NOT_IMPLEMENTED)
async def list_deploys(
    user: CurrentUser,
    session: SessionDep,
    workspace_id: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> DeployPage:
    """Newest first. Pass next_cursor back as ?cursor= for the next page."""
    await _check_member(session, user, workspace_id)
    raise _not_implemented()


@router.get("/{deploy_id}", responses=_NOT_IMPLEMENTED)
async def get_deploy(deploy_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> DeployDetail:
    """A deploy with its stages, plan (secrets masked), and final deploy record."""
    raise _not_implemented()
