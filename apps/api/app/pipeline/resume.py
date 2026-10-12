"""Which code resumes a run after an approval: a handler per run.agent (made for Phase 5).

decide_approval enqueues one job kind, "resume_run", for every run. The worker hands it to
`resume_run_job`, which picks the handler registered for the run's agent, or the generic
AgentRunner when none is. Deploy runs resume in the pipeline; Phase 5 registers Medic:

    from app.pipeline.resume import register_resume_handler
    register_resume_handler("medic", medic_resume_job)

A handler has the worker's job signature: `async (session_factory, payload) -> None`, where the
payload has `run_id` and `approval_id`.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select

from app.db.models import Run
from app.db.session import SessionFactory

ResumeHandler = Callable[[SessionFactory, dict[str, Any]], Awaitable[None]]

_handlers: dict[str, ResumeHandler] = {}


def register_resume_handler(agent: str, handler: ResumeHandler) -> None:
    """Resume runs of `agent` with `handler` (replaces an earlier registration)."""
    _handlers[agent] = handler


def resume_handler(agent: str | None) -> ResumeHandler:
    """The handler for a run's agent; AgentRunner's when none is registered."""
    if agent is not None and agent in _handlers:
        return _handlers[agent]
    from app.agents.base import run_agent_job

    return run_agent_job


async def resume_run_job(session_factory: SessionFactory, payload: dict[str, Any]) -> None:
    """Worker handler for "resume_run" jobs."""
    async with session_factory() as session:
        agent = await session.scalar(
            select(Run.agent).where(Run.id == uuid.UUID(payload["run_id"]))
        )
    await resume_handler(agent)(session_factory, payload)


def _register_builtin() -> None:
    from app.pipeline.runner import pipeline_job

    register_resume_handler("deploy", pipeline_job)


_register_builtin()
