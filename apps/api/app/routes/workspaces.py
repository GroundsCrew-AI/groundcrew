"""The signed-in user and their workspaces: /me, run lists, approvals inbox, overview counts."""

import base64
import binascii
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select

from app import deletion, quotas
from app.accounts import (
    NotFoundError,
    SignInError,
    change_password,
    member_workspace,
    sign_in_methods,
    user_workspaces,
)
from app.agents.medic.events import PENDING_FOLLOWUP_STATES
from app.audit import record
from app.config import Settings, get_settings
from app.db.models import (
    TERMINAL_RUN_STATUSES,
    Approval,
    ApprovalStatus,
    Incident,
    IncidentFollowUp,
    LlmWorkspaceUsage,
    Run,
    RunStatus,
    RunStep,
    User,
    Workspace,
)
from app.incidents.store import OPEN_STATUSES
from app.llm.budget import usage_totals, workspace_daily_cap, workspace_usage_today
from app.platform import is_platform_admin
from app.routes.deps import ClientIp, CurrentUser, SessionDep, SettingsDep
from app.routes.runs import rejected_counts
from app.routes.schemas import (
    ActivityDay,
    AgentActivity,
    ApprovalOut,
    MeOut,
    PasswordChange,
    ProfileUpdate,
    RunOut,
    RunPage,
    UserOut,
    WorkspaceActivity,
    WorkspaceOut,
    WorkspaceSummary,
)
from app.security.passwords import hash_password_async, password_problem

router = APIRouter(tags=["workspaces"])


def _start_of_day() -> datetime:
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


async def _check_member(session: SessionDep, user: User, workspace_id: uuid.UUID) -> None:
    try:
        await member_workspace(session, user, workspace_id)
    except NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found.") from None


async def _me(session: SessionDep, user: User, settings: Settings) -> MeOut:
    workspaces = await user_workspaces(session, user)
    return MeOut(
        user=UserOut.build(user, await sign_in_methods(session, user)),
        workspaces=[WorkspaceOut.build(w, r) for w, r in workspaces],
        password_auth=settings.password_auth,
        platform_admin=is_platform_admin(user, settings),
    )


@router.get("/me")
async def me(user: CurrentUser, session: SessionDep, settings: SettingsDep) -> MeOut:
    return await _me(session, user, settings)


@router.patch("/me")
async def update_me(
    body: ProfileUpdate, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> MeOut:
    """Change your display name."""
    user.name = body.name.strip() or user.name
    await session.commit()
    return await _me(session, user, settings)


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_my_password(
    body: PasswordChange, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> None:
    """Change your password, or add one to a GitHub or Google account (it then signs in with
    its email too). Every session ends, this one included: sign in again with the new one."""
    if not settings.password_auth:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Email sign-in is turned off on this server."
        )
    if not user.email:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Your account has no email address, so a password would not sign you in.",
        )
    problem = password_problem(
        body.new_password, min_length=settings.password_min_length, email=user.email
    )
    if problem:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, problem)
    try:
        await change_password(
            session, user, body.current_password, await hash_password_async(body.new_password)
        )
    except SignInError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Your current password is not right."
        ) from None


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
    pending_fixes = await session.scalar(
        select(func.count(func.distinct(IncidentFollowUp.incident_id))).where(
            IncidentFollowUp.workspace_id == workspace_id,
            IncidentFollowUp.state.in_(PENDING_FOLLOWUP_STATES),
        )
    )
    open_incidents = await session.scalar(
        select(func.count(Incident.id)).where(
            Incident.workspace_id == workspace_id, Incident.status.in_(OPEN_STATUSES)
        )
    )
    settings = settings or get_settings()
    usage_today, _ = await usage_totals(session)
    spent = await workspace_usage_today(session, workspace_id)
    spent_total = await session.scalar(
        select(func.coalesce(func.sum(LlmWorkspaceUsage.usd), 0)).where(
            LlmWorkspaceUsage.workspace_id == workspace_id
        )
    )
    return WorkspaceSummary(
        runs_today=int(runs_today or 0),
        pending_approvals=int(pending or 0),
        workspace_tokens_today=int(workspace_tokens or 0),
        budget_tokens_today=usage_today.tokens,
        daily_token_budget=settings.daily_token_budget,
        usd_today=float(spent.usd),
        daily_usd_cap=float(await workspace_daily_cap(session, workspace_id, settings)),
        usd_total=float(spent_total or 0),
        total_usd_cap=None,
        pending_permanent_fixes=int(pending_fixes or 0),
        open_incidents=int(open_incidents or 0),
    )


@router.get("/workspaces/{workspace_id}/activity")
async def activity(
    workspace_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    days: Annotated[int, Query(ge=7, le=90)] = 14,
) -> WorkspaceActivity:
    """Daily runs, outcomes, spend, and incidents for the overview, oldest day first."""
    await _check_member(session, user, workspace_id)
    first = _start_of_day() - timedelta(days=days - 1)
    run_day = func.date(func.timezone("UTC", Run.created_at))
    rows = (
        await session.execute(
            select(run_day, Run.agent, Run.status, func.count(Run.id))
            .where(Run.workspace_id == workspace_id, Run.created_at >= first)
            .group_by(run_day, Run.agent, Run.status)
        )
    ).all()
    series = {
        first.date() + timedelta(days=i): ActivityDay(
            day=first.date() + timedelta(days=i),
            runs={},
            succeeded=0,
            failed=0,
            usd=0.0,
            tokens=0,
            incidents_opened=0,
            incidents_resolved=0,
        )
        for i in range(days)
    }
    for day, agent, run_status, count in rows:
        point = series.get(day)
        if point is None:
            continue
        point.runs[agent] = point.runs.get(agent, 0) + count
        if run_status == RunStatus.succeeded:
            point.succeeded += count
        elif run_status in (RunStatus.failed, RunStatus.stopped):
            point.failed += count
    for usage in await session.scalars(
        select(LlmWorkspaceUsage).where(
            LlmWorkspaceUsage.workspace_id == workspace_id, LlmWorkspaceUsage.day >= first.date()
        )
    ):
        if point := series.get(usage.day):
            point.usd, point.tokens = float(usage.usd), int(usage.tokens)
    opened_day = func.date(func.timezone("UTC", Incident.created_at))
    for day, count in await session.execute(
        select(opened_day, func.count(Incident.id))
        .where(Incident.workspace_id == workspace_id, Incident.created_at >= first)
        .group_by(opened_day)
    ):
        if point := series.get(day):
            point.incidents_opened = count
    resolved_day = func.date(func.timezone("UTC", Incident.resolved_at))
    for day, count in await session.execute(
        select(resolved_day, func.count(Incident.id))
        .where(Incident.workspace_id == workspace_id, Incident.resolved_at >= first)
        .group_by(resolved_day)
    ):
        if point := series.get(day):
            point.incidents_resolved = count

    seconds = func.extract("epoch", Run.finished_at - Run.started_at)
    agents = [
        AgentActivity(
            agent=agent,
            runs=runs,
            succeeded=int(ok or 0),
            failed=int(bad or 0),
            tokens=int(tokens or 0),
            median_seconds=float(median) if median is not None else None,
        )
        for agent, runs, ok, bad, tokens, median in await session.execute(
            select(
                Run.agent,
                func.count(Run.id),
                func.count(Run.id).filter(Run.status == RunStatus.succeeded),
                func.count(Run.id).filter(Run.status.in_([RunStatus.failed, RunStatus.stopped])),
                func.sum(Run.prompt_tokens + Run.completion_tokens),
                func.percentile_cont(0.5).within_group(seconds),
            )
            .where(Run.workspace_id == workspace_id, Run.created_at >= first)
            .group_by(Run.agent)
            .order_by(func.count(Run.id).desc(), Run.agent)
        )
    ]
    open_by_severity = {
        severity or "unknown": count
        for severity, count in await session.execute(
            select(Incident.severity, func.count(Incident.id))
            .where(Incident.workspace_id == workspace_id, Incident.status.in_(OPEN_STATUSES))
            .group_by(Incident.severity)
        )
    }
    median_resolve = await session.scalar(
        select(
            func.percentile_cont(0.5).within_group(
                func.extract("epoch", Incident.resolved_at - Incident.created_at)
            )
        ).where(Incident.workspace_id == workspace_id, Incident.resolved_at >= first)
    )
    return WorkspaceActivity(
        days=list(series.values()),
        agents=agents,
        open_by_severity=open_by_severity,
        median_resolve_seconds=float(median_resolve) if median_resolve is not None else None,
    )


class QuotaOut(BaseModel):
    # tavily, builds, concurrent_runs, connections
    name: str
    label: str
    used: int
    limit: int
    # Counted per UTC day (resets at `resets_at`); otherwise a live count.
    daily: bool


class UsageOut(BaseModel):
    """What this workspace used of its limits, for Settings."""

    day: date
    resets_at: datetime
    usd_today: float
    daily_usd_cap: float
    # Groundcrew's own daily or total model spend limit is reached for every workspace.
    platform_paused: bool
    max_build_seconds: int
    quotas: list[QuotaOut]


_QUOTA_LABELS = {
    "tavily": "Web research calls today",
    "builds": "Deploy builds today",
    "concurrent_runs": "Runs going at once",
    "connections": "Connections",
}


@router.get("/workspaces/{workspace_id}/usage")
async def workspace_usage(
    workspace_id: uuid.UUID, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> UsageOut:
    try:
        workspace, _ = await member_workspace(session, user, workspace_id)
    except NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found.") from None
    today_all, total_all = await usage_totals(session)
    spent = await workspace_usage_today(session, workspace_id)
    used = await quotas.usage(session, workspace, settings)
    return UsageOut(
        day=quotas.today(),
        resets_at=quotas.next_reset(),
        usd_today=float(spent.usd),
        daily_usd_cap=float(await workspace_daily_cap(session, workspace_id, settings)),
        platform_paused=today_all.usd >= Decimal(str(settings.llm_daily_usd_cap))
        or total_all >= Decimal(str(settings.llm_total_usd_cap)),
        max_build_seconds=int(settings.build_timeout_seconds),
        quotas=[
            QuotaOut(
                name=name,
                label=_QUOTA_LABELS[name],
                used=q.used,
                limit=q.limit,
                daily=name in ("tavily", "builds"),
            )
            for name, q in used.items()
        ],
    )


class WorkspaceRef(BaseModel):
    id: uuid.UUID
    name: str


class AccountDeletionOut(BaseModel):
    """What deleting your account would do."""

    # Workspaces only you are in: deleted with everything in them.
    deletes: list[WorkspaceRef]
    # Shared workspaces you leave.
    leaves: list[WorkspaceRef]
    # Shared workspaces where you are the only owner: make someone else an owner first.
    blocked_by: list[WorkspaceRef]


class AccountDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Must be exactly "delete my account".
    confirm: str = Field(max_length=40)


def _deletion_out(plan: deletion.AccountPlan) -> AccountDeletionOut:
    def refs(items: list[Workspace]) -> list[WorkspaceRef]:
        return [WorkspaceRef(id=w.id, name=w.name) for w in items]

    return AccountDeletionOut(
        deletes=refs(plan.delete), leaves=refs(plan.leave), blocked_by=refs(plan.blocked)
    )


@router.get("/me/account-deletion")
async def account_deletion_preview(user: CurrentUser, session: SessionDep) -> AccountDeletionOut:
    return _deletion_out(await deletion.account_plan(session, user))


@router.delete(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={409: {"description": "You are the only owner of a shared workspace."}},
)
async def delete_my_account(
    body: AccountDelete, user: CurrentUser, session: SessionDep, ip: ClientIp
) -> None:
    """Delete your account: your sign-in identities, memberships, and the workspaces only you
    are in, with everything in them. Shared workspaces stay; your name stays on what you
    approved there. Signs you out everywhere."""
    if body.confirm.strip().lower() != "delete my account":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, 'Type "delete my account" to confirm.'
        )
    plan = await deletion.account_plan(session, user)
    if plan.blocked:
        names = ", ".join(w.name for w in plan.blocked)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"You are the only owner of {names}. Make someone else an owner there first, or "
            "delete the workspace.",
        )
    for workspace in plan.leave:
        record(
            session,
            workspace_id=workspace.id,
            actor=user,
            action="member.left",
            target_type="user",
            target_id=user.id,
            detail={"reason": "account_deleted"},
            ip=ip,
        )
    record(
        session,
        workspace_id=None,
        actor=user,
        action="account.deleted",
        target_type="user",
        target_id=user.id,
        detail={"workspaces_deleted": len(plan.delete)},
        ip=ip,
    )
    await deletion.delete_account(session, user, plan)
    await session.commit()
