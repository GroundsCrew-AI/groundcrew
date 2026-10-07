"""The on_call hook for agent runs: every model call becomes a run_steps row and adds to the
run's totals. SSE polls run_steps, so writing the step is what puts it on the live timeline.
"""

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Run
from app.llm.client import LLMResult, describe_result
from app.steps import record_step


class StepRecorder:
    """Bound to one run and the session that owns it. Commits after each call."""

    def __init__(self, session: AsyncSession, run: Run, agent: str) -> None:
        self.session = session
        self.run = run
        self.agent = agent

    async def __call__(self, result: LLMResult) -> None:
        run = self.run
        run.prompt_tokens += result.prompt_tokens
        run.completion_tokens += result.completion_tokens
        run.cost_usd = (run.cost_usd or Decimal(0)) + result.cost_usd
        run.ultra_equiv_usd = (run.ultra_equiv_usd or Decimal(0)) + result.ultra_equiv_usd
        await record_step(
            self.session,
            run,
            kind="llm",
            agent=self.agent,
            model_tier=result.tier,
            model_id=result.model_id,
            reasoning_mode=result.reasoning_mode,
            input_summary=result.input_summary,
            output_summary=describe_result(result),
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            latency_ms=result.latency_ms,
            cost_usd=result.cost_usd,
            ultra_equiv_usd=result.ultra_equiv_usd,
            fell_back=result.fell_back,
            escalated=result.escalated,
            reasoning_excerpt=result.reasoning,
        )
        await self.session.commit()
