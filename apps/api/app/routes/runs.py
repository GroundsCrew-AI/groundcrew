"""Runs: start a run, read it, stop it, and stream it live over SSE.

Every route here needs the signed-in user (internal JWT) except the SSE stream, which the
browser opens directly and which is authorized by a 60-second stream token instead
(POST /runs/{id}/stream-token). A run in a workspace the user is not a member of is a 404.

The SSE stream polls the database (default every 500 ms) instead of LISTEN/NOTIFY,
which Neon's connection pooler does not support.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import queue
from app.accounts import NotFoundError, member_workspace, require_member
from app.agents.medic.events import MEDIC_EVENT_NAMES
from app.agents.registry import AGENTS
from app.db.models import (
    TERMINAL_RUN_STATUSES,
    Approval,
    ApprovalStatus,
    Run,
    RunStatus,
    RunStep,
    StopReason,
    User,
)
from app.db.session import SessionFactory
from app.pipeline.events import DEPLOY_EVENT_NAMES
from app.routes.deps import CurrentUser, SessionDep, SessionFactoryDep, SettingsDep
from app.routes.schemas import (
    ApprovalOut,
    ApprovalStepEvent,
    RunCreate,
    RunCreated,
    RunDetail,
    RunOut,
    RunStatusEvent,
    StepOut,
    StreamToken,
)
from app.security.auth import AuthConfigError, AuthError, create_stream_token, verify_stream_token
from app.steps import record_step

router = APIRouter(prefix="/runs", tags=["runs"])

HEARTBEAT_SECONDS = 15.0
STOPPABLE = (RunStatus.queued, RunStatus.running, RunStatus.waiting_approval)


def _not_found(what: str = "Run") -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found.")


async def member_run(
    session: AsyncSession, user: User, run_id: uuid.UUID, *, for_update: bool = False
) -> Run:
    run = await session.get(Run, run_id, with_for_update=for_update)
    if run is None:
        raise _not_found()
    try:
        await require_member(session, user.id, run.workspace_id)
    except NotFoundError:
        raise _not_found() from None
    return run


async def rejected_counts(session: AsyncSession, run_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """How many actions people rejected, per run."""
    if not run_ids:
        return {}
    rows = await session.execute(
        select(Approval.run_id, func.count(Approval.id))
        .where(Approval.run_id.in_(run_ids), Approval.status == ApprovalStatus.rejected)
        .group_by(Approval.run_id)
    )
    return {run_id: int(count) for run_id, count in rows}


async def run_approvals(session: AsyncSession, run: Run) -> list[ApprovalOut]:
    rows = await session.execute(
        select(Approval, User)
        .outerjoin(User, User.id == Approval.decided_by)
        .where(Approval.run_id == run.id)
        .order_by(Approval.created_at)
    )
    return [ApprovalOut.build(a, run, u) for a, u in rows]


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_run(body: RunCreate, user: CurrentUser, session: SessionDep) -> RunCreated:
    if body.agent not in AGENTS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Unknown agent {body.agent!r}. Available: {', '.join(sorted(AGENTS))}",
        )
    try:
        await member_workspace(session, user, body.workspace_id)
    except NotFoundError:
        raise _not_found("Workspace") from None

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
async def get_run(run_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> RunDetail:
    run = await member_run(session, user, run_id)
    steps = await _steps_after(session, run_id, 0)
    approvals = await run_approvals(session, run)
    rejected = sum(1 for a in approvals if a.status == ApprovalStatus.rejected)
    return RunDetail(
        **RunOut.build(run, rejected_actions=rejected).model_dump(),
        steps=[StepOut.build(s) for s in steps],
        approvals=approvals,
    )


@router.post("/{run_id}/stream-token")
async def stream_token(
    run_id: uuid.UUID, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> StreamToken:
    """A 60-second token for GET /runs/{id}/events, bound to this run and user."""
    await member_run(session, user, run_id)
    token, expires_at = create_stream_token(user.id, run_id, settings)
    return StreamToken(token=token, expires_at=expires_at)


@router.post("/{run_id}/stop")
async def stop_run(run_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> RunOut:
    """Stop a queued, running, or waiting run. Its pending approvals are rejected.

    A running agent notices before its next model or tool call and exits without
    overwriting this status.
    """
    run = await member_run(session, user, run_id, for_update=True)
    if run.status not in STOPPABLE:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Run is already {run.status}.")

    now = datetime.now(UTC)
    pending = await session.scalars(
        select(Approval)
        .where(Approval.run_id == run.id, Approval.status == ApprovalStatus.pending)
        .with_for_update()
    )
    for approval in pending:
        approval.status = ApprovalStatus.rejected
        approval.decided_by = user.id
        approval.decided_at = now
    run.status = RunStatus.stopped
    run.stop_reason = StopReason.stopped_by_user
    run.finished_at = now
    await record_step(
        session, run, kind="stopped", output_summary=f"Stopped by {user.github_login or user.id}"
    )
    await session.commit()
    counts = await rejected_counts(session, [run.id])
    return RunOut.build(run, rejected_actions=counts.get(run.id, 0))


def _sse(event: str, data: BaseModel, event_id: int | None = None) -> str:
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"data: {data.model_dump_json()}")
    return "\n".join(lines) + "\n\n"


async def _step_event(session: AsyncSession, run: Run, step: RunStep) -> str:
    if step.kind in ("approval_requested", "approval_decided"):
        approval_out = None
        if step.approval_id is not None:
            row = (
                await session.execute(
                    select(Approval, User)
                    .outerjoin(User, User.id == Approval.decided_by)
                    .where(Approval.id == step.approval_id)
                )
            ).first()
            if row is not None:
                approval_out = ApprovalOut.build(row[0], run, row[1])
        payload = ApprovalStepEvent(step=StepOut.build(step), approval=approval_out)
        return _sse(step.kind, payload, step.seq)
    if step.kind in DEPLOY_EVENT_NAMES or step.kind in MEDIC_EVENT_NAMES:
        return _sse(step.kind, StepOut.build(step), step.seq)
    return _sse("step", StepOut.build(step), step.seq)


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
            state = RunStatusEvent.build(run)
            steps = await _steps_after(session, run_id, after_seq)
            events = [await _step_event(session, run, step) for step in steps]
            if steps:
                after_seq = steps[-1].seq

        for event in events:
            yield event
            last_sent = time.monotonic()
        if state != last_state:
            yield _sse("status", state)
            last_state = state
            last_sent = time.monotonic()
        if state.status in TERMINAL_RUN_STATUSES:
            yield _sse("done", state)
            return

        if await request.is_disconnected():
            return
        if time.monotonic() - last_sent >= HEARTBEAT_SECONDS:
            yield ": heartbeat\n\n"
            last_sent = time.monotonic()
        await asyncio.sleep(poll_seconds)


def _resume_after(header: str | None, query: str | None) -> int:
    for value in (header, query):
        if value and value.isdigit():
            return int(value)
    return 0


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
    token: Annotated[str, Query(description="Stream token from POST /runs/{id}/stream-token")],
    last_event_id: Annotated[str | None, Header()] = None,
    after: Annotated[
        str | None, Query(description="Resume after this step seq (when the header can't be set)")
    ] = None,
) -> StreamingResponse:
    """SSE: existing steps, then new ones and status changes until the run ends.

    Events: `step`, `approval_requested`, `approval_decided`, the deploy events
    (`stage_started`, `stage_finished`, `build_attempt`, `verify_result`, `live_url`,
    `rollback_offered`, `run_finished`), the incident events (`incident_opened`,
    `incident_updated`, `evidence_collected`, `memory_checked`, `research_done`,
    `diagnosis`, `fix_proposed`, `fix_applied`, `incident_resolved`, `incident_failed`;
    id = step seq), `status`, and a final `done`.
    See docs/events.md. A heartbeat comment every 15 s keeps proxies from closing the
    connection. Reconnects with Last-Event-ID (or ?after=) continue after that step.

    No request-scoped session here: FastAPI closes it only after the stream ends, which
    would hold a pooled connection idle in transaction for every open stream. Each poll
    uses its own short session instead.
    """
    try:
        user_id = verify_stream_token(token, run_id, settings)
    except AuthConfigError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from None

    async with session_factory() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise _not_found()
        try:
            # Still a member now, not just when the token was issued.
            await require_member(session, user_id, run.workspace_id)
        except NotFoundError:
            raise _not_found() from None

    stream = _event_stream(
        session_factory,
        run_id,
        request,
        _resume_after(last_event_id, after),
        settings.sse_poll_interval_ms / 1000,
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
