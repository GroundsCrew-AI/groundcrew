"""The signed-in user and their workspaces: /me, run lists, approvals inbox, overview counts."""

import base64
import binascii
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import and_, func, or_, select

from app.accounts import NotFoundError, member_workspace, user_workspaces
from app.config import get_settings
from app.db.models import TERMINAL_RUN_STATUSES, Approval, ApprovalStatus, Run, RunStep, User
from app.llm.budget import usage_totals
from app.routes.deps import CurrentUser, SessionDep, SettingsDep
from app.routes.runs import rejected_counts
from app.routes.schemas import (
    ApprovalOut,
    MeOut,
    RunOut,
    RunPage,
    UserOut,
    WorkspaceOut,
    WorkspaceSummary,
)

router = APIRouter(tags=["workspaces"])


def _start_of_day() -> datetime:
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


async def _check_member(session: SessionDep, user: User, workspace_id: uuid.UUID) -> None:
    try:
        await member_workspace(session, user, workspace_id)
    except NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found.") from None


@router.get("/me")
async def me(user: CurrentUser, session: SessionDep) -> MeOut:
    workspaces = await user_workspaces(session, user)
    return MeOut(
        user=UserOut.model_validate(user),
        workspaces=[WorkspaceOut.build(w, r) for w, r in workspaces],
    )


def _encode_cursor(run: Run) -> str:
    raw = f"{run.created_at.isoformat()}|{run.id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        created, _, run_id = raw.partition("|")
        return datetime.fromisoformat(created), uuid.UUID(run_id)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid cursor.") from None


@router.get("/workspaces/{workspace_id}/runs")
async def list_runs(
    workspace_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> RunPage:
    """Newest first. Pass next_cursor back as ?cursor= for the next page."""
    await _check_member(session, user, workspace_id)
    query = (
        select(Run)
        .where(Run.workspace_id == workspace_id)
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(limit + 1)
    )
    if cursor:
        created, run_id = _decode_cursor(cursor)
        query = query.where(
            or_(Run.created_at < created, and_(Run.created_at == created, Run.id < run_id))
        )
    runs = list(await session.scalars(query))
    has_more = len(runs) > limit
    runs = runs[:limit]
    rejected = await rejected_counts(session, [r.id for r in runs])
    return RunPage(
        items=[RunOut.build(r, rejected_actions=rejected.get(r.id, 0)) for r in runs],
        next_cursor=_encode_cursor(runs[-1]) if has_more else None,
    )


@router.get("/workspaces/{workspace_id}/approvals")
async def list_approvals(
    workspace_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    status_filter: Annotated[ApprovalStatus | None, Query(alias="status")] = None,
    decided: Annotated[
        bool | None, Query(description="true: approved or rejected only (history)")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[ApprovalOut]:
    """Pending approvals only list runs that can still resume (not stopped or finished)."""
    await _check_member(session, user, workspace_id)
    query = (
        select(Approval, Run, User)
        .join(Run, Run.id == Approval.run_id)
        .outerjoin(User, User.id == Approval.decided_by)
        .where(Approval.workspace_id == workspace_id)
        .limit(limit)
    )
    if status_filter is not None:
        query = query.where(Approval.status == status_filter)
        if status_filter == ApprovalStatus.pending:
            query = query.where(Run.status.not_in(TERMINAL_RUN_STATUSES))
    if decided is True:
        query = query.where(Approval.status != ApprovalStatus.pending)
    # Inbox: oldest pending first. History: most recently decided first.
    if status_filter == ApprovalStatus.pending:
        query = query.order_by(Approval.created_at)
    else:
        query = query.order_by(func.coalesce(Approval.decided_at, Approval.created_at).desc())
    rows = await session.execute(query)
    return [ApprovalOut.build(a, r, u) for a, r, u in rows]


@router.get("/workspaces/{workspace_id}/summary")
async def summary(
    workspace_id: uuid.UUID, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> WorkspaceSummary:
    await _check_member(session, user, workspace_id)
    today = _start_of_day()
    runs_today = await session.scalar(
        select(func.count(Run.id)).where(Run.workspace_id == workspace_id, Run.created_at >= today)
    )
    pending = await session.scalar(
        select(func.count(Approval.id))
        .join(Run, Run.id == Approval.run_id)
        .where(
            Approval.workspace_id == workspace_id,
            Approval.status == ApprovalStatus.pending,
            Run.status.not_in(TERMINAL_RUN_STATUSES),
        )
    )
    workspace_tokens = await session.scalar(
        select(
            func.coalesce(func.sum(RunStep.prompt_tokens), 0)
            + func.coalesce(func.sum(RunStep.completion_tokens), 0)
        )
        .join(Run, Run.id == RunStep.run_id)
        .where(Run.workspace_id == workspace_id, RunStep.created_at >= today)
    )
    settings = settings or get_settings()
    usage_today, usage_total = await usage_totals(session)
    return WorkspaceSummary(
        runs_today=int(runs_today or 0),
        pending_approvals=int(pending or 0),
        workspace_tokens_today=int(workspace_tokens or 0),
        budget_tokens_today=usage_today.tokens,
        daily_token_budget=settings.daily_token_budget,
        usd_today=float(usage_today.usd),
        daily_usd_cap=settings.llm_daily_usd_cap,
        usd_total=float(usage_total),
        total_usd_cap=settings.llm_total_usd_cap,
    )
