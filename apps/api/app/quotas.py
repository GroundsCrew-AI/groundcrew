"""Per-workspace quotas: what one workspace may use, so no tenant can use up the platform.

- Daily counts (workspace_quota_usage, reset at 00:00 UTC): Tavily calls (`tavily`) and deploy
  builds (`builds`). `take` adds one only while the count is under the limit, in one statement,
  so concurrent workers cannot both take the last one.
- Live counts: runs a person started that are still queued or running, and connections.

Limits come from settings (QUOTA_*), or from `workspaces.settings["quotas"]` for one workspace,
which only a platform operator sets (`python -m app.admin_cli quota`); workspace owners cannot
raise them.
Model spend has its own caps (app.llm.budget).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    Connection,
    Incident,
    Run,
    RunStatus,
    Workspace,
    WorkspaceQuotaUsage,
)

Quota = Literal["tavily", "builds", "concurrent_runs", "connections"]

QUOTAS_KEY = "quotas"
_SETTING: dict[Quota, str] = {
    "tavily": "quota_tavily_per_workspace_day",
    "builds": "quota_builds_per_workspace_day",
    "concurrent_runs": "quota_concurrent_runs_per_workspace",
    "connections": "quota_connections_per_workspace",
}
_WHAT: dict[Quota, str] = {
    "tavily": "web research calls today",
    "builds": "deploy builds today",
    "concurrent_runs": "runs going at once",
    "connections": "connections",
}
ACTIVE = (RunStatus.queued, RunStatus.running, RunStatus.waiting_approval)
# Agents whose runs a person starts; Medic counts only for incidents a person reported, so
# the watcher's investigations are never held back.
USER_AGENTS = ("echo_ops", "deploy", "discovery")


class QuotaExceededError(Exception):
    """A workspace reached a quota. Routes answer 429 with `code` quota_exceeded."""

    def __init__(self, quota: Quota, limit: int, resets_at: datetime | None) -> None:
        self.quota, self.limit, self.resets_at = quota, limit, resets_at
        when = " It resets at 00:00 UTC." if resets_at else ""
        super().__init__(f"This workspace reached its limit of {limit} {_WHAT[quota]}.{when}")


def today() -> date:
    return datetime.now(UTC).date()


def next_reset(day: date | None = None) -> datetime:
    return datetime.combine((day or today()) + timedelta(days=1), time(0), tzinfo=UTC)


def limit_for(workspace: Workspace | None, quota: Quota, settings: Settings) -> int:
    """The workspace's limit: an operator's override for it, else the platform setting."""
    default = int(getattr(settings, _SETTING[quota]))
    overrides: Any = ((workspace.settings or {}) if workspace else {}).get(QUOTAS_KEY) or {}
    value = overrides.get(quota) if isinstance(overrides, dict) else None
    return int(value) if isinstance(value, int) and value >= 0 else default


async def _limit(session: AsyncSession, workspace_id: uuid.UUID, quota: Quota, s: Settings) -> int:
    return limit_for(await session.get(Workspace, workspace_id), quota, s)


async def take(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    quota: Literal["tavily", "builds"],
    settings: Settings,
) -> int:
    """Count one use for today and return the new count, or raise QuotaExceededError when the
    workspace is at its limit. The caller commits."""
    limit = await _limit(session, workspace_id, quota, settings)
    day = today()
    table = WorkspaceQuotaUsage.__table__
    stmt = (
        insert(WorkspaceQuotaUsage)
        .values(workspace_id=workspace_id, day=day, kind=quota, count=1)
        .on_conflict_do_update(
            index_elements=["workspace_id", "day", "kind"],
            set_={"count": table.c.count + 1},
            where=table.c.count < limit,
        )
        .returning(table.c.count)
    )
    count = (await session.execute(stmt)).scalar_one_or_none() if limit > 0 else None
    if count is None:
        raise QuotaExceededError(quota, limit, next_reset(day))
    return int(count)


async def used_today(session: AsyncSession, workspace_id: uuid.UUID, quota: str) -> int:
    count = await session.scalar(
        select(WorkspaceQuotaUsage.count).where(
            WorkspaceQuotaUsage.workspace_id == workspace_id,
            WorkspaceQuotaUsage.day == today(),
            WorkspaceQuotaUsage.kind == quota,
        )
    )
    return int(count or 0)


async def active_runs(session: AsyncSession, workspace_id: uuid.UUID) -> int:
    """Runs a person started that are still going (the watcher's are left out)."""
    return int(
        await session.scalar(
            select(func.count(Run.id))
            .outerjoin(Incident, Incident.id == Run.incident_id)
            .where(
                Run.workspace_id == workspace_id,
                Run.status.in_(ACTIVE),
                or_(
                    Run.agent.in_(USER_AGENTS),
                    and_(Run.agent == "medic", Incident.detected_by == "user"),
                ),
            )
        )
        or 0
    )


async def connection_count(session: AsyncSession, workspace_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count(Connection.id)).where(Connection.workspace_id == workspace_id)
        )
        or 0
    )


async def check_can_start_run(
    session: AsyncSession, workspace_id: uuid.UUID, settings: Settings
) -> None:
    limit = await _limit(session, workspace_id, "concurrent_runs", settings)
    if await active_runs(session, workspace_id) >= limit:
        raise QuotaExceededError("concurrent_runs", limit, None)


async def check_can_add_connection(
    session: AsyncSession, workspace_id: uuid.UUID, settings: Settings
) -> None:
    limit = await _limit(session, workspace_id, "connections", settings)
    if await connection_count(session, workspace_id) >= limit:
        raise QuotaExceededError("connections", limit, None)


@dataclass(frozen=True)
class QuotaUse:
    used: int
    limit: int


async def usage(
    session: AsyncSession, workspace: Workspace, settings: Settings
) -> dict[Quota, QuotaUse]:
    ws = workspace.id
    return {
        "tavily": QuotaUse(
            await used_today(session, ws, "tavily"), limit_for(workspace, "tavily", settings)
        ),
        "builds": QuotaUse(
            await used_today(session, ws, "builds"), limit_for(workspace, "builds", settings)
        ),
        "concurrent_runs": QuotaUse(
            await active_runs(session, ws), limit_for(workspace, "concurrent_runs", settings)
        ),
        "connections": QuotaUse(
            await connection_count(session, ws), limit_for(workspace, "connections", settings)
        ),
    }
