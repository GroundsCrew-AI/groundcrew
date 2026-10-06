"""Job worker: claims jobs from Postgres and dispatches them by kind.

Run: python -m app.worker  (make worker, or the `worker` compose service)
SIGTERM / SIGINT stop it after the current job finishes.
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


class UnknownJobKindError(Exception):
    pass


def default_handlers() -> dict[str, Handler]:
    # Agent jobs (run_agent, resume_run) are registered with the agent loop.
    return {}


class Worker:
    def __init__(
        self,
        session_factory: SessionFactory,
        handlers: Mapping[str, Handler],
        *,
        worker_id: str | None = None,
        poll_interval: float = 1.0,
        lock_timeout_seconds: int = 600,
    ) -> None:
        self.session_factory = session_factory
        self.handlers = handlers
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.poll_interval = poll_interval
        self.lock_timeout_seconds = lock_timeout_seconds

    async def run_once(self) -> bool:
        """Claim and run one job. Returns False when there was nothing to do."""
        async with self.session_factory() as session:
            job = await queue.claim(session, self.worker_id, self.lock_timeout_seconds)
        if job is None:
            return False

        token = job_id_var.set(str(job.id))
        try:
            logger.info("job started", extra={"kind": job.kind, "attempt": job.attempts})
            handler = self.handlers.get(job.kind)
            try:
                if handler is None:
                    raise UnknownJobKindError(f"No handler for job kind {job.kind!r}")
                await handler(self.session_factory, job.payload)
            except Exception as exc:
                retry = not isinstance(exc, UnknownJobKindError)
                async with self.session_factory() as session:
                    status = await queue.mark_failed(
                        session, job.id, f"{type(exc).__name__}: {exc}", retry=retry
                    )
                logger.exception("job failed", extra={"kind": job.kind, "status": status})
            else:
                async with self.session_factory() as session:
                    await queue.mark_done(session, job.id)
                logger.info("job done", extra={"kind": job.kind})
        finally:
            job_id_var.reset(token)
        return True

    async def run(self, stop: asyncio.Event) -> None:
        logger.info("worker started", extra={"worker_id": self.worker_id})
        while not stop.is_set():
            try:
                worked = await self.run_once()
            except Exception:
                # Usually the database is down or not migrated yet. Keep going.
                logger.exception("worker loop error")
                worked = False
            if not worked:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_interval)
        logger.info("worker stopped", extra={"worker_id": self.worker_id})


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
    )
    try:
        await worker.run(stop)
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
