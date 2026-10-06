"""Runs: start an agent run, read it with its steps, and stream steps live over SSE.

The SSE stream polls the database (default every 500 ms) instead of LISTEN/NOTIFY,
which Neon's connection pooler does not support.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import queue
from app.agents.registry import AGENTS
from app.db.models import TERMINAL_RUN_STATUSES, Run, RunStatus, RunStep, Workspace
from app.db.session import SessionFactory
from app.routes.deps import SessionDep, SessionFactoryDep, SettingsDep
from app.routes.schemas import RunCreate, RunCreated, RunOut, RunStatusEvent, RunWithSteps, StepOut

router = APIRouter(prefix="/runs", tags=["runs"])

HEARTBEAT_SECONDS = 15.0


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_run(body: RunCreate, session: SessionDep) -> RunCreated:
    if body.agent not in AGENTS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Unknown agent {body.agent!r}. Available: {', '.join(sorted(AGENTS))}",
        )
    if await session.get(Workspace, body.workspace_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found.")

    run = Run(workspace_id=body.workspace_id, agent=body.agent, goal=body.goal)
    session.add(run)
    await session.flush()
    # Same transaction: the run and its job exist together or not at all.
    await queue.enqueue(session, "run_agent", {"run_id": str(run.id)})
    await session.commit()
    return RunCreated(id=run.id, status=RunStatus.queued)


async def _steps_after(session: AsyncSession, run_id: uuid.UUID, after_seq: int) -> list[RunStep]:
    result = await session.scalars(
        select(RunStep)
        .where(RunStep.run_id == run_id, RunStep.seq > after_seq)
        .order_by(RunStep.seq)
    )
    return list(result)


@router.get("/{run_id}")
async def get_run(run_id: uuid.UUID, session: SessionDep) -> RunWithSteps:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found.")
    steps = await _steps_after(session, run_id, 0)
    return RunWithSteps(
        **RunOut.model_validate(run).model_dump(),
        steps=[StepOut.model_validate(s) for s in steps],
    )


def _sse(event: str, data: BaseModel, event_id: int | None = None) -> str:
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"data: {data.model_dump_json()}")
    return "\n".join(lines) + "\n\n"


async def _event_stream(
    session_factory: SessionFactory,
    run_id: uuid.UUID,
    request: Request,
    after_seq: int,
    poll_seconds: float,
) -> AsyncIterator[str]:
    last_state: RunStatusEvent | None = None
    last_sent = time.monotonic()
    while True:
        async with session_factory() as session:
            # Read the run before its steps: if it is finished, every step it wrote is
            # already committed and the steps query below sees them.
            run = await session.get(Run, run_id)
            if run is None:
                return
            state = RunStatusEvent.model_validate(run, from_attributes=True)
            steps = await _steps_after(session, run_id, after_seq)

        for step in steps:
            yield _sse("step", StepOut.model_validate(step), step.seq)
            after_seq = step.seq
            last_sent = time.monotonic()
        if state != last_state:
            yield _sse("status", state)
            last_state = state
            last_sent = time.monotonic()
        if state.status in TERMINAL_RUN_STATUSES:
            yield _sse("end", state)
            return

        if await request.is_disconnected():
            return
        if time.monotonic() - last_sent >= HEARTBEAT_SECONDS:
            yield ": keep-alive\n\n"
            last_sent = time.monotonic()
        await asyncio.sleep(poll_seconds)


@router.get(
    "/{run_id}/events",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def run_events(
    run_id: uuid.UUID,
    request: Request,
    session_factory: SessionFactoryDep,
    settings: SettingsDep,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """SSE: existing steps, then new steps and status changes until the run ends.

    Events: `step` (id = seq), `status`, and a final `end`. Reconnects with
    Last-Event-ID continue after that step.

    No request-scoped session here: FastAPI closes it only after the stream ends, which
    would hold a pooled connection idle in transaction for every open stream. Each poll
    uses its own short session instead.
    """
    async with session_factory() as session:
        exists = await session.get(Run, run_id) is not None
    if not exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found.")
    after_seq = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0
    stream = _event_stream(
        session_factory, run_id, request, after_seq, settings.sse_poll_interval_ms / 1000
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
