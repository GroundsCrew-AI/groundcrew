"""Dev only: what collect_evidence returns for some resources (docs/phase5-interfaces.md).

`GET /workspaces/{id}/evidence?resource=<id>&resource=<id>&since=&until=` runs the same readers
Medic uses, read-only, and returns the text per source. Answers 404 unless APP_ENV=dev.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from app.accounts import NotFoundError, member_workspace
from app.agents.medic.events import ChangeMarker, ResourceRef
from app.routes.deps import CurrentUser, SessionDep, SessionFactoryDep, SettingsDep
from app.telemetry.collect import collect

router = APIRouter(tags=["dev"])

DEFAULT_WINDOW = timedelta(minutes=60)


class EvidenceOut(BaseModel):
    # collect_evidence's text per source, plus `timeline`.
    sources: dict[str, str]
    truncated: dict[str, bool]
    changes: list[ChangeMarker]
    affected: list[ResourceRef]
    neighbors: list[ResourceRef]
    notes: list[str]


@router.get("/workspaces/{workspace_id}/evidence", include_in_schema=False)
async def workspace_evidence(
    workspace_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    session_factory: SessionFactoryDep,
    settings: SettingsDep,
    resource: Annotated[list[uuid.UUID], Query(min_length=1, max_length=20)],
    since: datetime | None = None,
    until: datetime | None = None,
) -> EvidenceOut:
    if settings.app_env != "dev":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    try:
        await member_workspace(session, user, workspace_id)
    except NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found.") from None
    end = until or datetime.now(UTC)
    start = since or end - DEFAULT_WINDOW
    if start >= end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "since must be before until.")
    evidence = await collect(session_factory, workspace_id, resource, start, end, settings=settings)
    return EvidenceOut(
        sources=evidence.texts(),
        truncated=evidence.truncated,
        changes=evidence.changes,
        affected=evidence.affected,
        neighbors=evidence.neighbors,
        notes=evidence.notes,
    )
