"""Job worker: claims jobs from Postgres and dispatches them by kind.

Run: python -m app.worker  (make worker, or the `worker` compose service)

While a job runs, a heartbeat refreshes its lock every JOB_LOCK_TIMEOUT_SECONDS / 4. If the
lock is lost anyway (for example the worker was paused for longer than the timeout and
another worker took the job), the job is cancelled here so a run never executes twice.

SIGTERM / SIGINT: the current job gets WORKER_SHUTDOWN_GRACE_SECONDS to finish. After that
it is cancelled and put back in the queue without using up an attempt; the run resumes from
its last saved message in the next worker.
"""

import asyncio
import contextlib
import logging
import signal
import socket
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from app import queue
from app.config import get_settings
from app.db.session import SessionFactory, get_engine, get_session_factory
from app.log import job_id_var, setup_logging

logger = logging.getLogger("groundcrew.worker")

Handler = Callable[[SessionFactory, dict[str, Any]], Awaitable[None]]


def default_handlers() -> dict[str, Handler]:
    # Imported here so `app.queue` users do not load every agent and tool.
    from app.agents.base import run_agent_job
    from app.agents.medic.jobs import (
        NO_FIX_JOB,
        VERIFY_JOB,
        investigate_incident_job,
        no_fix_deadline_job,
        rediagnose_incident_job,
        verify_incident_job,
    )
    from app.discovery.engine import discovery_job
    from app.incidents.store import INVESTIGATE_JOB, REDIAGNOSE_JOB
    from app.incidents.watcher import WATCH_JOB, watch_incidents_job
    from app.notifications import NOTIFY_JOB, notify_job
    from app.pipeline.resume import resume_run_job
    from app.pipeline.runner import DEPLOY_JOB, pipeline_job

    return {
        "run_agent": run_agent_job,
        # By run.agent (app.pipeline.resume): deploy runs in the pipeline, Medic once Phase 5
        # registers it, everything else in the agent loop.
        "resume_run": resume_run_job,
        "discover": discovery_job,
        "discover_workspace": discovery_job,
        DEPLOY_JOB: pipeline_job,
        INVESTIGATE_JOB: investigate_incident_job,
        VERIFY_JOB: verify_incident_job,
        REDIAGNOSE_JOB: rediagnose_incident_job,
        NO_FIX_JOB: no_fix_deadline_job,
        WATCH_JOB: watch_incidents_job,
        NOTIFY_JOB: notify_job,
    }


class Worker:
    def __init__(
        self,
        session_factory: SessionFactory,
        handlers: Mapping[str, Handler],
        *,
        worker_id: str | None = None,
        poll_interval: float = 1.0,
        lock_timeout_seconds: int = 600,
        heartbeat_seconds: float | None = None,
        shutdown_grace_seconds: float = 20.0,
    ) -> None:
        self.session_factory = session_factory
        self.handlers = handlers
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.poll_interval = poll_interval
        self.lock_timeout_seconds = lock_timeout_seconds
        self.heartbeat_seconds = heartbeat_seconds or max(lock_timeout_seconds / 4, 1.0)
        self.shutdown_grace_seconds = shutdown_grace_seconds

    async def _heartbeat(self, job_id: uuid.UUID, handler_task: asyncio.Task[None]) -> None:
        """Keep the job lock fresh. Cancels the handler if another worker took the job."""
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            try:
                async with self.session_factory() as session:
                    held = await queue.heartbeat(session, job_id, self.worker_id)
            except Exception:
                # The database blipped. Try again next beat; the lock timeout is much longer.
                logger.warning("job heartbeat failed", exc_info=True)
                continue
            if not held:
                logger.error("job lock lost, cancelling job", extra={"worker_id": self.worker_id})
                handler_task.cancel()
                return

    async def run_once(self, stop: asyncio.Event | None = None) -> bool:
        """Claim and run one job. Returns False when there was nothing to do."""
        async with self.session_factory() as session:
            job = await queue.claim(session, self.worker_id, self.lock_timeout_seconds)
        if job is None:
            return False

        token = job_id_var.set(str(job.id))
        try:
            logger.info("job started", extra={"kind": job.kind, "attempt": job.attempts})
            await self._run_job(job.id, job.kind, job.payload, stop)
        finally:
            job_id_var.reset(token)
        return True

    async def _run_job(
        self,
        job_id: uuid.UUID,
        kind: str,
        payload: dict[str, Any],
        stop: asyncio.Event | None,
    ) -> None:
        handler = self.handlers.get(kind)
        if handler is None:
            async with self.session_factory() as session:
                await queue.mark_failed(
                    session,
                    job_id,
                    f"UnknownJobKindError: No handler for job kind {kind!r}",
                    retry=False,
                    owner=self.worker_id,
                )
            logger.error("job failed: unknown kind", extra={"kind": kind})
            return

        async def call_handler() -> None:
            await handler(self.session_factory, payload)

        handler_task: asyncio.Task[None] = asyncio.create_task(call_handler())
        heartbeat_task = asyncio.create_task(self._heartbeat(job_id, handler_task))
        stop_task = asyncio.create_task(stop.wait()) if stop is not None else None
        stopped = False
        try:
            waiting: set[asyncio.Future[Any]] = {handler_task}
            if stop_task is not None:
                waiting.add(stop_task)
            done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            if handler_task not in done:
                # Shutting down: give the job a grace period, then cancel it.
                stopped = True
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        asyncio.shield(handler_task), timeout=self.shutdown_grace_seconds
                    )
                handler_task.cancel()
            # Wait for the task to settle without raising its exception here; _settle reads it.
            await asyncio.wait({handler_task})
        finally:
            heartbeat_task.cancel()
            if stop_task is not None:
                stop_task.cancel()
            await asyncio.gather(
                heartbeat_task, *([stop_task] if stop_task else []), return_exceptions=True
            )

        await self._settle(job_id, kind, handler_task, stopped)

    async def _settle(
        self, job_id: uuid.UUID, kind: str, task: asyncio.Task[None], stopped: bool
    ) -> None:
        """Record how the job ended. Only the lock owner can change the job."""
        async with self.session_factory() as session:
            if task.cancelled():
                if stopped and await queue.release(session, job_id, owner=self.worker_id):
                    logger.warning("job released for another worker", extra={"kind": kind})
                else:
                    logger.warning("job cancelled after its lock was lost", extra={"kind": kind})
                return
            exc = task.exception()
            if exc is not None:
                status = await queue.mark_failed(
                    session, job_id, f"{type(exc).__name__}: {exc}", owner=self.worker_id
                )
                logger.error(
                    "job failed",
                    extra={"kind": kind, "status": status},
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                return
            if await queue.mark_done(session, job_id, owner=self.worker_id):
                logger.info("job done", extra={"kind": kind})
            else:
                logger.warning("job finished but its lock was lost", extra={"kind": kind})

    async def run(self, stop: asyncio.Event) -> None:
        logger.info("worker started", extra={"worker_id": self.worker_id})
        while not stop.is_set():
            try:
                worked = await self.run_once(stop)
            except Exception:
                # Usually the database is down or not migrated yet. Keep going.
                logger.exception("worker loop error")
                worked = False
            if not worked:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_interval)
        logger.info("worker stopped", extra={"worker_id": self.worker_id})


MAINTENANCE_INTERVAL_SECONDS = 60.0


async def maintenance_pass(session_factory: SessionFactory) -> None:
    """Re-arm the incident watcher (its chain ends if a worker dies mid-pass) and prune old
    jobs. Safe to run from every worker at once."""
    settings = get_settings()
    if settings.incident_watch_enabled:
        from app.incidents.watcher import ensure_watch_job

        await ensure_watch_job(session_factory)
    async with session_factory() as session:
        await queue.prune(
            session,
            done_older_than_days=settings.job_retention_days,
            failed_older_than_days=settings.failed_job_retention_days,
        )


async def maintain(session_factory: SessionFactory, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await maintenance_pass(session_factory)
        except Exception:
            logger.exception("worker maintenance failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=MAINTENANCE_INTERVAL_SECONDS)


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    worker = Worker(
        get_session_factory(),
        default_handlers(),
        poll_interval=settings.worker_poll_interval_seconds,
        lock_timeout_seconds=settings.job_lock_timeout_seconds,
        shutdown_grace_seconds=settings.worker_shutdown_grace_seconds,
    )
    settings.require_production_ready()
    housekeeping = asyncio.create_task(maintain(worker.session_factory, stop))
    try:
        await worker.run(stop)
    finally:
        housekeeping.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await housekeeping
        await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
