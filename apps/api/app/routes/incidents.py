"""Incidents: Medic investigates, proposes fixes, applies them after approval, and verifies
(docs/phase5-interfaces.md).

POST /incidents opens (or joins) an incident and starts Medic; the reads serve the incident with
Medic's state. Every route authenticates and checks workspace membership (404 for non-members,
like everywhere else). Live progress is on the Medic run's event stream.
"""

import base64
import binascii
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, or_, select

from app.accounts import NotFoundError, member_workspace
from app.agents.medic.events import IncidentStatus, Severity
from app.connectors.github import GitHubApp, get_github_app
from app.db.models import Incident
from app.incidents import views
from app.incidents.store import ResourceNotFoundError, open_incident
from app.routes.deps import CurrentUser, SessionDep
from app.routes.incident_schemas import (
    IncidentCreate,
    IncidentCreated,
    IncidentDetail,
    IncidentPage,
)

router = APIRouter(prefix="/incidents", tags=["incidents"])


async def _check_member(
    session: SessionDep, user: CurrentUser, workspace_id: uuid.UUID, what: str = "Workspace"
) -> None:
    try:
        await member_workspace(session, user, workspace_id)
    except NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found.") from None


def get_github() -> GitHubApp | None:
    """The GitHub App when configured (refreshes fix PR states); tests override it."""
    app = get_github_app()
    return app if app.configured() else None


GitHubDep = Annotated[GitHubApp | None, Depends(get_github)]


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


@router.get("")
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
    query = (
        select(Incident)
        .where(Incident.workspace_id == workspace_id)
        .order_by(Incident.created_at.desc(), Incident.id.desc())
        .limit(limit + 1)
    )
    if status_filter is not None:
        query = query.where(Incident.status == status_filter)
    if severity is not None:
        query = query.where(Incident.severity == severity)
    if resource_id is not None:
        query = query.where(Incident.affected_resource_ids.contains([resource_id]))
    if since is not None:
        query = query.where(Incident.created_at >= since)
    if cursor:
        created, incident_id = _decode_cursor(cursor)
        query = query.where(
            or_(
                Incident.created_at < created,
                and_(Incident.created_at == created, Incident.id < incident_id),
            )
        )
    rows = list(await session.scalars(query))
    has_more = len(rows) > limit
    rows = rows[:limit]
    return IncidentPage(
        items=await views.summaries(session, rows),
        next_cursor=_encode_cursor(rows[-1]) if has_more else None,
    )


@router.get("/{incident_id}")
async def get_incident(
    incident_id: uuid.UUID, user: CurrentUser, session: SessionDep, github: GitHubDep
) -> IncidentDetail:
    """An incident with its signals, evidence (cited by id), change markers, root cause, fixes
    with their approvals and PR links, verify result, memory, and research."""
    incident = await session.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found.")
    await _check_member(session, user, incident.workspace_id, what="Incident")
    if github is not None:
        await views.refresh_pull_requests(incident, github)
        await session.commit()
    return await views.detail(session, incident)


def _encode_cursor(incident: Incident) -> str:
    raw = f"{incident.created_at.isoformat()}|{incident.id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        created, _, incident_id = raw.partition("|")
        return datetime.fromisoformat(created), uuid.UUID(incident_id)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid cursor.") from None
