"""Token and call budgets, checked before every LLM call (PROJECT_CONTEXT.md section 7.7).

check_budget also returns the max_tokens for the next call, so one call cannot go far past
MAX_TOKENS_PER_RUN or DAILY_TOKEN_BUDGET: the prompt size is estimated (about 4 characters
per token) and the completion is capped to what is left. If what is left is below
LLM_MIN_COMPLETION_TOKENS, the run stops instead of making a call that would be cut off.

The daily check reads usage without a lock. That is exact with one worker (jobs run one at a
time); with several workers, the overshoot is at most one capped call per worker.
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

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


def estimate_prompt_tokens(
    messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]] = ()
) -> int:
    """Rough prompt size: about 4 characters per token. Only used to cap max_tokens."""
    chars = len(json.dumps(list(messages), default=str)) + len(json.dumps(list(tools)))
    return chars // 4 + 1


async def check_budget(
    session: AsyncSession,
    run: Run,
    tier: Tier,
    settings: Settings | None = None,
    *,
    prompt_tokens: int = 0,
) -> int:
    """Raise BudgetExceededError if the next call is not allowed, else return its max_tokens.

    `prompt_tokens` is the estimated size of the prompt about to be sent.
    """
    settings = settings or get_settings()
    cap = settings.llm_max_tokens

    run_limit = settings.max_tokens_per_run
    if run.total_tokens >= run_limit:
        raise BudgetExceededError(
            f"Run used {run.total_tokens} tokens (MAX_TOKENS_PER_RUN={run_limit})."
        )
    cap = min(cap, run_limit - run.total_tokens - prompt_tokens)
    if cap < settings.llm_min_completion_tokens:
        raise BudgetExceededError(
            f"Run used {run.total_tokens} tokens and the next prompt is about {prompt_tokens}; "
            f"not enough left for another call (MAX_TOKENS_PER_RUN={run_limit})."
        )

    if settings.daily_token_budget is not None:
        daily = settings.daily_token_budget
        used = await tokens_used_today(session)
        if used >= daily:
            raise BudgetExceededError(f"{used} tokens used today (DAILY_TOKEN_BUDGET={daily}).")
        cap = min(cap, daily - used - prompt_tokens)
        if cap < settings.llm_min_completion_tokens:
            raise BudgetExceededError(
                f"{used} tokens used today; not enough left for another call "
                f"(DAILY_TOKEN_BUDGET={daily})."
            )
    if tier == Tier.ultra and settings.max_ultra_calls_per_incident is not None:
        calls = await ultra_calls(session, run)
        if calls >= settings.max_ultra_calls_per_incident:
            raise BudgetExceededError(
                f"{calls} Ultra calls already made "
                f"(MAX_ULTRA_CALLS_PER_INCIDENT={settings.max_ultra_calls_per_incident})."
            )
    return cap
