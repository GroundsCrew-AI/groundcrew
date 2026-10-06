"""Token and call budgets, checked before every LLM call (PROJECT_CONTEXT.md section 7.7)."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import Run, RunStep
from app.llm.router import Tier


class BudgetExceededError(Exception):
    """The agent loop turns this into stop_reason="budget"."""


async def tokens_used_today(session: AsyncSession) -> int:
    start_of_day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    total = await session.scalar(
        select(
            func.coalesce(func.sum(RunStep.prompt_tokens), 0)
            + func.coalesce(func.sum(RunStep.completion_tokens), 0)
        ).where(RunStep.created_at >= start_of_day)
    )
    return int(total or 0)


async def ultra_calls(session: AsyncSession, run: Run) -> int:
    """Ultra calls in this run's incident, or in this run when it has no incident."""
    query = select(func.count(RunStep.id)).where(
        RunStep.kind == "llm", RunStep.model_tier == Tier.ultra
    )
    if run.incident_id is not None:
        query = query.join(Run, Run.id == RunStep.run_id).where(Run.incident_id == run.incident_id)
    else:
        query = query.where(RunStep.run_id == run.id)
    return int(await session.scalar(query) or 0)


async def check_budget(
    session: AsyncSession, run: Run, tier: Tier, settings: Settings | None = None
) -> None:
    settings = settings or get_settings()
    if run.total_tokens >= settings.max_tokens_per_run:
        limit = settings.max_tokens_per_run
        raise BudgetExceededError(
            f"Run used {run.total_tokens} tokens (MAX_TOKENS_PER_RUN={limit})."
        )
    if settings.daily_token_budget is not None:
        used = await tokens_used_today(session)
        if used >= settings.daily_token_budget:
            raise BudgetExceededError(
                f"{used} tokens used today (DAILY_TOKEN_BUDGET={settings.daily_token_budget})."
            )
    if tier == Tier.ultra and settings.max_ultra_calls_per_incident is not None:
        calls = await ultra_calls(session, run)
        if calls >= settings.max_ultra_calls_per_incident:
            raise BudgetExceededError(
                f"{calls} Ultra calls already made "
                f"(MAX_ULTRA_CALLS_PER_INCIDENT={settings.max_ultra_calls_per_incident})."
            )
