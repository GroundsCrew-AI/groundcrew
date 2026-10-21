"""Spend guards, checked before every Token Factory call (PROJECT_CONTEXT.md section 7.7).

With about $30 of credits for the whole hackathon these are not optional:

- RunBudget: per-run caps on USD (LLM_RUN_USD_CAP), tokens (MAX_TOKENS_PER_RUN), and Ultra
  calls (MAX_ULTRA_CALLS_PER_RUN, MAX_ULTRA_CALLS_PER_INCIDENT). Loaded from the runs row, so a
  run that pauses for approval and resumes in another job keeps its spend.
- UsageStore: spend per UTC day in the llm_usage table, shared by every worker and script.
  LLM_DAILY_USD_CAP and DAILY_TOKEN_BUDGET apply per day, LLM_TOTAL_USD_CAP to all days: it is
  the hard stop. With a workspace, its own spend (llm_workspace_usage) has its own daily cap:
  the workspace's setting, else LLM_WORKSPACE_DAILY_USD_CAP, else LLM_DAILY_USD_CAP, so one
  workspace cannot use up another's budget.

Each check also caps the next call's max_tokens to what is left, so one call cannot go far
past a token budget. If what is left is below LLM_MIN_COMPLETION_TOKENS, the run stops
instead of making a call that would be cut off.

Usage is read without a lock. Exact with one worker; with several workers or concurrent
calls, the overshoot is at most one call each.
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import LlmUsage, LlmWorkspaceUsage, Run, RunStep, Workspace
from app.db.session import SessionFactory
from app.llm.models import Tier


class BudgetExceededError(Exception):
    """A cap was hit. The agent stops with stop_reason=budget; `cap` names which one."""

    def __init__(self, cap: str, detail: str) -> None:
        super().__init__(f"{cap}: {detail}")
        self.cap = cap
        self.detail = detail


@dataclass(frozen=True)
class Usage:
    usd: Decimal
    tokens: int


class UsageStore(Protocol):
    async def today(self) -> Usage: ...
    async def total_usd(self) -> Decimal: ...
    async def add(self, usd: Decimal, tokens: int) -> None: ...
    # This store's workspace's spend today, or None when it is not bound to a workspace.
    async def workspace_today(self) -> Usage | None: ...


def _today() -> date:
    return datetime.now(UTC).date()


class PgUsageStore:
    """llm_usage (and llm_workspace_usage for a workspace) in Postgres. Each method uses its
    own short session and commits."""

    def __init__(
        self, session_factory: SessionFactory, workspace_id: uuid.UUID | None = None
    ) -> None:
        self.session_factory = session_factory
        self.workspace_id = workspace_id

    async def workspace_today(self) -> Usage | None:
        if self.workspace_id is None:
            return None
        async with self.session_factory() as session:
            row = await session.get(LlmWorkspaceUsage, (self.workspace_id, _today()))
            return Usage(row.usd, row.tokens) if row else Usage(Decimal(0), 0)

    async def today(self) -> Usage:
        async with self.session_factory() as session:
            row = await session.get(LlmUsage, _today())
            return Usage(row.usd, row.tokens) if row else Usage(Decimal(0), 0)

    async def total_usd(self) -> Decimal:
        async with self.session_factory() as session:
            total = await session.scalar(select(func.coalesce(func.sum(LlmUsage.usd), 0)))
            return Decimal(total or 0)

    async def add(self, usd: Decimal, tokens: int) -> None:
        stmt = insert(LlmUsage).values(day=_today(), usd=usd, tokens=tokens, calls=1)
        stmt = stmt.on_conflict_do_update(
            index_elements=[LlmUsage.day],
            set_={
                "usd": LlmUsage.usd + stmt.excluded.usd,
                "tokens": LlmUsage.tokens + stmt.excluded.tokens,
                "calls": LlmUsage.calls + 1,
                "updated_at": func.now(),
            },
        )
        async with self.session_factory() as session:
            await session.execute(stmt)
            if self.workspace_id is not None:
                ws = insert(LlmWorkspaceUsage).values(
                    workspace_id=self.workspace_id, day=_today(), usd=usd, tokens=tokens, calls=1
                )
                ws = ws.on_conflict_do_update(
                    index_elements=[LlmWorkspaceUsage.workspace_id, LlmWorkspaceUsage.day],
                    set_={
                        "usd": LlmWorkspaceUsage.usd + ws.excluded.usd,
                        "tokens": LlmWorkspaceUsage.tokens + ws.excluded.tokens,
                        "calls": LlmWorkspaceUsage.calls + 1,
                        "updated_at": func.now(),
                    },
                )
                await session.execute(ws)
            await session.commit()


@dataclass
class MemoryUsageStore:
    """In-process usage, for unit tests only. Never use it for real calls."""

    usd_today: Decimal = Decimal(0)
    tokens_today: int = 0
    usd_before_today: Decimal = Decimal(0)
    # Set to bind the store to a (pretend) workspace.
    workspace_usd_today: Decimal | None = None

    async def workspace_today(self) -> Usage | None:
        if self.workspace_usd_today is None:
            return None
        return Usage(self.workspace_usd_today, 0)

    async def today(self) -> Usage:
        return Usage(self.usd_today, self.tokens_today)

    async def total_usd(self) -> Decimal:
        return self.usd_before_today + self.usd_today

    async def add(self, usd: Decimal, tokens: int) -> None:
        self.usd_today += usd
        self.tokens_today += tokens
        if self.workspace_usd_today is not None:
            self.workspace_usd_today += usd


def estimate_prompt_tokens(
    messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]] = ()
) -> int:
    """Rough prompt size: about 4 characters per token. Only used to cap max_tokens."""
    chars = len(json.dumps(list(messages), default=str)) + len(json.dumps(list(tools)))
    return chars // 4 + 1


@dataclass
class RunBudget:
    """Caps for one run, and what it has used so far."""

    usage: UsageStore
    settings: Settings = field(default_factory=get_settings)
    spent_usd: Decimal = Decimal(0)
    tokens: int = 0
    ultra_calls: int = 0
    # Ultra calls already made in this run's incident (other runs included). None: no incident.
    incident_ultra_calls: int | None = None
    ultra_equiv_usd: Decimal = Decimal(0)
    # This run's workspace's daily cap (checked when the usage store has a workspace).
    workspace_daily_usd_cap: Decimal | None = None

    @classmethod
    async def load(
        cls,
        session: AsyncSession,
        run: Run,
        usage: UsageStore,
        settings: Settings | None = None,
    ) -> "RunBudget":
        settings = settings or get_settings()
        is_ultra_llm = (RunStep.kind == "llm") & (RunStep.model_tier == Tier.ultra)
        ultra_calls = await session.scalar(
            select(func.count(RunStep.id)).where(RunStep.run_id == run.id, is_ultra_llm)
        )
        incident_calls = None
        if run.incident_id is not None:
            incident_calls = await session.scalar(
                select(func.count(RunStep.id))
                .join(Run, Run.id == RunStep.run_id)
                .where(Run.incident_id == run.incident_id, is_ultra_llm)
            )
        return cls(
            usage=usage,
            settings=settings,
            workspace_daily_usd_cap=await workspace_daily_cap(session, run.workspace_id, settings),
            spent_usd=run.cost_usd or Decimal(0),
            tokens=run.total_tokens,
            ultra_calls=int(ultra_calls or 0),
            incident_ultra_calls=None if incident_calls is None else int(incident_calls),
            ultra_equiv_usd=run.ultra_equiv_usd or Decimal(0),
        )

    def check(self, tier: Tier) -> None:
        """Per-run caps. Raises BudgetExceededError."""
        s = self.settings
        if self.spent_usd >= Decimal(str(s.llm_run_usd_cap)):
            raise BudgetExceededError(
                "run_usd_cap", f"run spent ${self.spent_usd} (LLM_RUN_USD_CAP={s.llm_run_usd_cap})"
            )
        if self.tokens >= s.max_tokens_per_run:
            raise BudgetExceededError(
                "run_token_cap",
                f"run used {self.tokens} tokens (MAX_TOKENS_PER_RUN={s.max_tokens_per_run})",
            )
        if tier == Tier.ultra:
            if self.ultra_calls >= s.max_ultra_calls_per_run:
                raise BudgetExceededError(
                    "ultra_call_cap",
                    f"{self.ultra_calls} Ultra calls in this run "
                    f"(MAX_ULTRA_CALLS_PER_RUN={s.max_ultra_calls_per_run})",
                )
            limit = s.max_ultra_calls_per_incident
            if limit is not None and (self.incident_ultra_calls or 0) >= limit:
                raise BudgetExceededError(
                    "incident_ultra_cap",
                    f"{self.incident_ultra_calls} Ultra calls in this incident "
                    f"(MAX_ULTRA_CALLS_PER_INCIDENT={limit})",
                )

    async def check_usage(self) -> Usage:
        """Daily and total caps from llm_usage. Returns today's usage."""
        s = self.settings
        total = await self.usage.total_usd()
        if total >= Decimal(str(s.llm_total_usd_cap)):
            raise BudgetExceededError(
                "total_usd_cap",
                f"Groundcrew reached its total model spend limit (${total} of "
                f"${s.llm_total_usd_cap}). Agents are paused until the operator raises it.",
            )
        today = await self.usage.today()
        if today.usd >= Decimal(str(s.llm_daily_usd_cap)):
            raise BudgetExceededError(
                "daily_usd_cap",
                f"Groundcrew reached today's model spend limit for all workspaces (${today.usd} "
                f"of ${s.llm_daily_usd_cap}). It resets at 00:00 UTC.",
            )
        cap = self.workspace_daily_usd_cap
        if cap is not None:
            workspace = await self.usage.workspace_today()
            if workspace is not None and workspace.usd >= cap:
                raise BudgetExceededError(
                    "workspace_daily_usd_cap",
                    f"${workspace.usd} spent today in this workspace, which has a daily limit "
                    f"of ${cap}. It resets at 00:00 UTC.",
                )
        if s.daily_token_budget is not None and today.tokens >= s.daily_token_budget:
            raise BudgetExceededError(
                "daily_token_cap",
                f"Groundcrew reached today's model token limit ({today.tokens} of "
                f"{s.daily_token_budget}). It resets at 00:00 UTC.",
            )
        return today

    def completion_cap(self, wanted: int, prompt_tokens: int, today: Usage) -> int:
        """max_tokens for the next call: `wanted`, cut to what the token budgets leave."""
        s = self.settings
        floor = min(s.llm_min_completion_tokens, wanted)
        run_left = s.max_tokens_per_run - self.tokens - prompt_tokens
        if run_left < floor:
            raise BudgetExceededError(
                "run_token_cap",
                f"run used {self.tokens} tokens and the next prompt is about {prompt_tokens}; "
                f"not enough left for an answer (MAX_TOKENS_PER_RUN={s.max_tokens_per_run})",
            )
        cap = min(wanted, run_left)
        if s.daily_token_budget is not None:
            daily_left = s.daily_token_budget - today.tokens - prompt_tokens
            if daily_left < floor:
                raise BudgetExceededError(
                    "daily_token_cap",
                    f"{today.tokens} tokens used today and the next prompt is about "
                    f"{prompt_tokens}; not enough left (DAILY_TOKEN_BUDGET={s.daily_token_budget})",
                )
            cap = min(cap, daily_left)
        return cap

    def record(self, tier: Tier, cost: Decimal, tokens: int, ultra_equiv: Decimal) -> None:
        self.spent_usd += cost
        self.tokens += tokens
        self.ultra_equiv_usd += ultra_equiv
        if tier == Tier.ultra:
            self.ultra_calls += 1
            if self.incident_ultra_calls is not None:
                self.incident_ultra_calls += 1

    def record_spend(self, cost: Decimal, tokens: int) -> None:
        """Spend that is not a chat call (embeddings, rerank): no tier, no Ultra equivalent."""
        self.spent_usd += cost
        self.tokens += tokens

    @property
    def savings_pct(self) -> float:
        """How much cheaper than running every call on Ultra."""
        if self.ultra_equiv_usd <= 0:
            return 0.0
        return float(100 * (1 - self.spent_usd / self.ultra_equiv_usd))


async def workspace_daily_cap(
    session: AsyncSession, workspace_id: uuid.UUID, settings: Settings | None = None
) -> Decimal:
    """The workspace's daily USD cap: the platform's per-workspace limit
    (LLM_WORKSPACE_DAILY_USD_CAP, else LLM_DAILY_USD_CAP), or the owner's lower setting. An owner
    can lower it, never raise it."""
    from app.workspace_settings import get_config

    settings = settings or get_settings()
    workspace = await session.get(Workspace, workspace_id)
    own = get_config(workspace).daily_usd_cap if workspace is not None else None
    default = settings.llm_workspace_daily_usd_cap
    platform = default if default is not None else settings.llm_daily_usd_cap
    value = min(own, platform) if own is not None else platform
    return Decimal(str(value))


async def workspace_usage_today(session: AsyncSession, workspace_id: uuid.UUID) -> Usage:
    row = await session.get(LlmWorkspaceUsage, (workspace_id, _today()))
    return Usage(row.usd, row.tokens) if row else Usage(Decimal(0), 0)


async def tokens_used_today(session: AsyncSession) -> int:
    """Tokens used today across all runs (from llm_usage)."""
    row = await session.get(LlmUsage, _today())
    return row.tokens if row else 0


async def usage_totals(session: AsyncSession) -> tuple[Usage, Decimal]:
    """(today's usage, total USD) for overview screens."""
    row = await session.get(LlmUsage, _today())
    total = await session.scalar(select(func.coalesce(func.sum(LlmUsage.usd), 0)))
    today = Usage(row.usd, row.tokens) if row else Usage(Decimal(0), 0)
    return today, Decimal(total or 0)
