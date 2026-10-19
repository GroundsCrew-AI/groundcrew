"""Postgres job queue: `jobs` table claimed with SELECT ... FOR UPDATE SKIP LOCKED.

A claimed job is marked running and committed right away, so the row lock is short and
two workers can never claim the same job. While a job runs, its worker refreshes locked_at
(heartbeat), so only a job whose worker died becomes claimable again after
JOB_LOCK_TIMEOUT_SECONDS. Only the worker that holds the lock can finish or release a job.

When a job for an agent run gives up for good, the run is failed too, so it never stays
"running" with nothing left to resume it.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TERMINAL_RUN_STATUSES, Job, JobStatus, Run, RunStatus, StopReason
from app.security.redact import summarize
from app.steps import record_step

MAX_ATTEMPTS = 3
# Retry delay after attempt n is BACKOFF_BASE * 4**(n - 1): 5 s, then 20 s.
BACKOFF_BASE = timedelta(seconds=5)


def _now() -> datetime:
    return datetime.now(UTC)


async def enqueue(
    session: AsyncSession,
    kind: str,
    payload: dict[str, Any],
    run_after: datetime | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> Job:
    """Add a job to the session. The caller commits, so the job lands with its related rows."""
    job = Job(
        kind=kind,
        payload=payload,
        run_after=run_after or _now(),
        max_attempts=max_attempts,
    )
    session.add(job)
    await session.flush()
    return job


async def claim(session: AsyncSession, worker_id: str, lock_timeout_seconds: int) -> Job | None:
    """Claim the next due job, or return None. Commits the claim."""
    while True:
        now = _now()
        stale_before = now - timedelta(seconds=lock_timeout_seconds)
        job = await session.scalar(
            select(Job)
            .where(
                or_(
                    and_(Job.status == JobStatus.queued, Job.run_after <= now),
                    and_(Job.status == JobStatus.running, Job.locked_at < stale_before),
                )
            )
            .order_by(Job.run_after, Job.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            await session.rollback()
            return None

        if job.status == JobStatus.running and job.attempts >= job.max_attempts:
            # Its worker died on the last attempt. Give up instead of running it again.
            job.status = JobStatus.failed
            job.last_error = "Lock expired on the final attempt (worker stopped mid-job)."
            job.locked_at = None
            await fail_job_run(session, job, job.last_error)
            await session.commit()
            continue

        job.status = JobStatus.running
        job.attempts += 1
        job.locked_at = now
        job.locked_by = worker_id
        await session.commit()
        return job


async def heartbeat(session: AsyncSession, job_id: uuid.UUID, worker_id: str) -> bool:
    """Refresh the lock on a running job. False means this worker no longer holds it."""
    result = await session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.running, Job.locked_by == worker_id)
        .values(locked_at=_now())
    )
    await session.commit()
    return bool(getattr(result, "rowcount", 0))


async def _owned_job(session: AsyncSession, job_id: uuid.UUID, owner: str | None) -> Job | None:
    """The job, locked, if `owner` still holds it (or no owner check was asked for)."""
    job = await session.get(Job, job_id, with_for_update=True, populate_existing=True)
    if job is None:
        return None
    if owner is not None and (job.status != JobStatus.running or job.locked_by != owner):
        await session.rollback()
        return None
    return job


async def mark_done(session: AsyncSession, job_id: uuid.UUID, *, owner: str | None = None) -> bool:
    """Mark the job done. Returns False (and changes nothing) if `owner` lost the lock."""
    job = await _owned_job(session, job_id, owner)
    if job is None:
        return False
    job.status = JobStatus.done
    job.locked_at = None
    job.last_error = None
    await session.commit()
    return True


async def mark_failed(
    session: AsyncSession,
    job_id: uuid.UUID,
    error: str,
    *,
    retry: bool = True,
    owner: str | None = None,
) -> JobStatus | None:
    """Record a failure. Requeue with backoff while attempts remain, else mark failed.

    Returns None (and changes nothing) if `owner` lost the lock.
    """
    job = await _owned_job(session, job_id, owner)
    if job is None:
        return None
    job.last_error = summarize(error)
    job.locked_at = None
    if retry and job.attempts < job.max_attempts:
        job.status = JobStatus.queued
        job.run_after = _now() + BACKOFF_BASE * 4 ** (job.attempts - 1)
    else:
        job.status = JobStatus.failed
        await fail_job_run(session, job, job.last_error)
    await session.commit()
    return job.status


async def release(session: AsyncSession, job_id: uuid.UUID, *, owner: str) -> bool:
    """Put a job back in the queue without using up an attempt (worker shutting down)."""
    job = await _owned_job(session, job_id, owner)
    if job is None:
        return False
    job.status = JobStatus.queued
    job.attempts = max(job.attempts - 1, 0)
    job.run_after = _now()
    job.locked_at = None
    job.locked_by = None
    await session.commit()
    return True


async def fail_job_run(session: AsyncSession, job: Job, error: str | None) -> None:
    """If the job drives an agent run that is not finished, fail the run. The caller commits."""
    run_id = job.payload.get("run_id") if isinstance(job.payload, dict) else None
    if not run_id:
        return
    try:
        run = await session.get(Run, uuid.UUID(str(run_id)), with_for_update=True)
    except ValueError:
        return
    if run is None or run.status in TERMINAL_RUN_STATUSES:
        return
    message = summarize(f"Job {job.kind} gave up after {job.attempts} attempts: {error}", 1000)
    await record_step(session, run, kind="error", output_summary=message)
    run.status = RunStatus.failed
    run.stop_reason = StopReason.error
    run.error = message
    run.finished_at = _now()


async def prune(
    session: AsyncSession, *, done_older_than_days: int, failed_older_than_days: int
) -> int:
    """Delete finished jobs older than the retention (by last update). Commits. Returns how
    many rows went. Queued and running jobs are never touched."""
    now = _now()
    result = await session.execute(
        delete(Job).where(
            or_(
                and_(
                    Job.status == JobStatus.done,
                    Job.updated_at < now - timedelta(days=done_older_than_days),
                ),
                and_(
                    Job.status == JobStatus.failed,
                    Job.updated_at < now - timedelta(days=failed_older_than_days),
                ),
            )
        )
    )
    await session.commit()
    return int(getattr(result, "rowcount", 0) or 0)
