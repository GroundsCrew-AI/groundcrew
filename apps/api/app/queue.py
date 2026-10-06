"""Postgres job queue: `jobs` table claimed with SELECT ... FOR UPDATE SKIP LOCKED.

A claimed job is marked running and committed right away, so the row lock is short and
two workers can never claim the same job. A job left running by a crashed worker becomes
claimable again after JOB_LOCK_TIMEOUT_SECONDS.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Job, JobStatus
from app.security.redact import summarize

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
            await session.commit()
            continue

        job.status = JobStatus.running
        job.attempts += 1
        job.locked_at = now
        job.locked_by = worker_id
        await session.commit()
        return job


async def mark_done(session: AsyncSession, job_id: uuid.UUID) -> None:
    job = await session.get_one(Job, job_id, with_for_update=True)
    job.status = JobStatus.done
    job.locked_at = None
    job.last_error = None
    await session.commit()


async def mark_failed(
    session: AsyncSession, job_id: uuid.UUID, error: str, *, retry: bool = True
) -> JobStatus:
    """Record a failure. Requeue with backoff while attempts remain, else mark failed."""
    job = await session.get_one(Job, job_id, with_for_update=True)
    job.last_error = summarize(error)
    job.locked_at = None
    if retry and job.attempts < job.max_attempts:
        job.status = JobStatus.queued
        job.run_after = _now() + BACKOFF_BASE * 4 ** (job.attempts - 1)
    else:
        job.status = JobStatus.failed
    await session.commit()
    return job.status
