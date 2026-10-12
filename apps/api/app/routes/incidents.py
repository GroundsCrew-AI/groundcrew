"""Incidents: Medic investigates, proposes fixes, applies them after approval, and verifies
(docs/phase5-interfaces.md).

POST /incidents works since M2 (incident row, dedupe, Medic run). The read routes answer 501
until M4. Every route authenticates and checks workspace membership (404 for non-members, like
everywhere else).
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status

from app.accounts import NotFoundError, member_workspace
from app.agents.medic.events import IncidentStatus, Severity
from app.incidents.store import ResourceNotFoundError, open_incident
from app.routes.deps import CurrentUser, SessionDep
from app.routes.incident_schemas import (
    IncidentCreate,
    IncidentCreated,
    IncidentDetail,
    IncidentPage,
)

router = APIRouter(prefix="/incidents", tags=["incidents"])

_NOT_IMPLEMENTED: dict[int | str, dict[str, Any]] = {
    501: {"description": "Incidents are not wired up yet."}
}


def _not_implemented() -> HTTPException:
    return HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "Incidents are not available yet.")


async def _check_member(session: SessionDep, user: CurrentUser, workspace_id: uuid.UUID) -> None:
    try:
        await member_workspace(session, user, workspace_id)
    except NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found.") from None


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    responses={404: {"description": "Workspace or a resource not found for this user."}},
)
async def create_incident(
    body: IncidentCreate, user: CurrentUser, session: SessionDep
) -> IncidentCreated:
    """Report an incident and start Medic. Returns at once; follow the run's event stream.

    One open incident per group of related resources: reporting resources an open incident
    already covers returns that incident (`deduped: true`).
    """
    await _check_member(session, user, body.workspace_id)
    try:
        opened = await open_incident(
            session,
            workspace_id=body.workspace_id,
            resource_ids=body.resource_ids,
            detected_by="user",
            signals=[],
            title=body.title,
            description=body.description,
            severity=body.severity,
            since=body.since,
        )
    except ResourceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    return IncidentCreated(
        incident_id=opened.incident.id, run_id=opened.run.id, deduped=opened.deduped
    )


@router.get("", responses=_NOT_IMPLEMENTED)
async def list_incidents(
    user: CurrentUser,
    session: SessionDep,
    workspace_id: uuid.UUID,
    status_filter: Annotated[IncidentStatus | None, Query(alias="status")] = None,
    severity: Severity | None = None,
    resource_id: Annotated[
        uuid.UUID | None, Query(alias="resource", description="Incidents affecting this resource")
    ] = None,
    since: Annotated[datetime | None, Query(description="Opened at or after this time")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> IncidentPage:
    """Newest first. Filters combine with AND. Pass next_cursor back as ?cursor= for the next
    page."""
    await _check_member(session, user, workspace_id)
    raise _not_implemented()


@router.get("/{incident_id}", responses=_NOT_IMPLEMENTED)
async def get_incident(
    incident_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> IncidentDetail:
    """An incident with its root cause (cited), fixes, and verify result."""
    raise _not_implemented()
