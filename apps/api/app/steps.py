"""Record run steps. Every summary is redacted and truncated before it is stored."""

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Run, RunStep
from app.security.redact import summarize


async def record_step(
    session: AsyncSession,
    run: Run,
    *,
    kind: str,
    agent: str | None = None,
    tool_name: str | None = None,
    input_summary: str | None = None,
    output_summary: str | None = None,
    model_tier: str | None = None,
    model_id: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    latency_ms: int | None = None,
    cost_usd: Decimal | None = None,
    approval_id: uuid.UUID | None = None,
    reasoning_mode: str | None = None,
    ultra_equiv_usd: Decimal | None = None,
    fell_back: bool = False,
    escalated: bool = False,
    reasoning_excerpt: str | None = None,
) -> RunStep:
    """Add the next step for this run. One worker runs a run at a time, so max(seq)+1 is safe;
    the (run_id, seq) unique constraint catches it if that ever breaks."""
    last_seq = await session.scalar(
        select(func.coalesce(func.max(RunStep.seq), 0)).where(RunStep.run_id == run.id)
    )
    step = RunStep(
        run_id=run.id,
        seq=int(last_seq or 0) + 1,
        agent=agent or run.agent,
        kind=kind,
        tool_name=tool_name,
        input_summary=summarize(input_summary),
        output_summary=summarize(output_summary),
        model_tier=model_tier,
        model_id=model_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        approval_id=approval_id,
        reasoning_mode=reasoning_mode,
        ultra_equiv_usd=ultra_equiv_usd,
        fell_back=fell_back,
        escalated=escalated,
        # The trace is shown in the UI only: redacted and capped like every summary.
        reasoning_excerpt=summarize(reasoning_excerpt),
    )
    session.add(step)
    await session.flush()
    return step
