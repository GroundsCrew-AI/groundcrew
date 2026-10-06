import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from app import queue
from app.db.models import Job, JobStatus, Run, RunStatus, RunStep, StopReason, User, Workspace
from app.db.session import SessionFactory
from app.worker import Worker

pytestmark = pytest.mark.anyio


async def _enqueue(sf: SessionFactory, n: int = 1, **kwargs: Any) -> list[uuid.UUID]:
    async with sf() as session:
        jobs = [await queue.enqueue(session, "test", {"i": i}, **kwargs) for i in range(n)]
        await session.commit()
        return [j.id for j in jobs]


async def _get(sf: SessionFactory, job_id: uuid.UUID) -> Job:
    async with sf() as session:
        return await session.get_one(Job, job_id)


async def test_claim_marks_running(session_factory: SessionFactory) -> None:
    [job_id] = await _enqueue(session_factory)
    async with session_factory() as session:
        job = await queue.claim(session, "w1", 600)
    assert job is not None and job.id == job_id
    stored = await _get(session_factory, job_id)
    assert stored.status == JobStatus.running
    assert stored.attempts == 1
    assert stored.locked_by == "w1"

    async with session_factory() as session:
        assert await queue.claim(session, "w2", 600) is None


async def test_locked_row_is_skipped(session_factory: SessionFactory) -> None:
    await _enqueue(session_factory)
    async with session_factory() as holder:
        # Another worker holds the row lock mid-claim.
        await holder.scalar(select(Job).with_for_update())
        async with session_factory() as session:
            assert await queue.claim(session, "w2", 600) is None
        await holder.rollback()
    async with session_factory() as session:
        assert await queue.claim(session, "w2", 600) is not None


async def test_concurrent_workers_never_double_claim(session_factory: SessionFactory) -> None:
    job_ids = await _enqueue(session_factory, n=30)
    claimed: list[uuid.UUID] = []

    async def drain(worker_id: str) -> None:
        while True:
            async with session_factory() as session:
                job = await queue.claim(session, worker_id, 600)
            if job is None:
                return
            claimed.append(job.id)
            await asyncio.sleep(0)

    await asyncio.gather(*(drain(f"w{i}") for i in range(4)))
    assert len(claimed) == len(set(claimed)) == len(job_ids)


async def test_future_job_is_not_claimed(session_factory: SessionFactory) -> None:
    await _enqueue(session_factory, run_after=datetime.now(UTC) + timedelta(minutes=5))
    async with session_factory() as session:
        assert await queue.claim(session, "w1", 600) is None


async def test_failure_retries_with_backoff_then_fails(session_factory: SessionFactory) -> None:
    [job_id] = await _enqueue(session_factory)
    for attempt in range(1, 4):
        async with session_factory() as session:
            await session.execute(
                update(Job).where(Job.id == job_id).values(run_after=datetime.now(UTC))
            )
            await session.commit()
            job = await queue.claim(session, "w1", 600)
        assert job is not None and job.attempts == attempt
        async with session_factory() as session:
            status = await queue.mark_failed(session, job_id, "boom")
        stored = await _get(session_factory, job_id)
        if attempt < 3:
            assert status == JobStatus.queued
            assert stored.run_after > datetime.now(UTC)
        else:
            assert status == JobStatus.failed
    assert (await _get(session_factory, job_id)).last_error == "boom"


async def test_stale_running_job_is_reclaimed(session_factory: SessionFactory) -> None:
    [job_id] = await _enqueue(session_factory)
    async with session_factory() as session:
        assert await queue.claim(session, "dead-worker", 600) is not None
        await session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(locked_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await session.commit()
        job = await queue.claim(session, "w2", 600)
    assert job is not None and job.locked_by == "w2" and job.attempts == 2


async def test_worker_dispatches_and_retries(session_factory: SessionFactory) -> None:
    seen: list[dict[str, Any]] = []
    calls = 0

    async def ok(_sf: SessionFactory, payload: dict[str, Any]) -> None:
        seen.append(payload)

    async def flaky(_sf: SessionFactory, payload: dict[str, Any]) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("token=abcdef123456 went wrong")

    worker = Worker(session_factory, {"ok": ok, "flaky": flaky})
    async with session_factory() as session:
        ok_job = await queue.enqueue(session, "ok", {"x": 1})
        flaky_job = await queue.enqueue(session, "flaky", {})
        unknown_job = await queue.enqueue(session, "nope", {})
        await session.commit()

    while await worker.run_once():
        pass

    assert seen == [{"x": 1}]
    assert (await _get(session_factory, ok_job.id)).status == JobStatus.done
    flaky_stored = await _get(session_factory, flaky_job.id)
    assert flaky_stored.status == JobStatus.queued  # retried later with backoff
    assert "abcdef123456" not in (flaky_stored.last_error or "")
    assert (await _get(session_factory, unknown_job.id)).status == JobStatus.failed


async def test_worker_stops_on_signal_event(session_factory: SessionFactory) -> None:
    worker = Worker(session_factory, {}, poll_interval=10)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=2)


async def _run_for_job(sf: SessionFactory) -> tuple[uuid.UUID, uuid.UUID]:
    """A running agent run with its job, as a worker would leave it."""
    async with sf() as session:
        user = User(email=f"{uuid.uuid4().hex[:6]}@test.local", name="T")
        session.add(user)
        await session.flush()
        ws = Workspace(name="T", owner_id=user.id)
        session.add(ws)
        await session.flush()
        run = Run(workspace_id=ws.id, agent="echo_ops", goal="g", status=RunStatus.running)
        session.add(run)
        await session.flush()
        job = await queue.enqueue(session, "run_agent", {"run_id": str(run.id)})
        await session.commit()
        return run.id, job.id


async def test_dead_job_on_final_attempt_fails_its_run(session_factory: SessionFactory) -> None:
    run_id, job_id = await _run_for_job(session_factory)
    async with session_factory() as session:
        await session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status=JobStatus.running,
                attempts=3,
                locked_by="dead",
                locked_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await session.commit()
        assert await queue.claim(session, "w2", 600) is None
    async with session_factory() as session:
        run = await session.get_one(Run, run_id)
        steps = (await session.scalars(select(RunStep).where(RunStep.run_id == run_id))).all()
    assert (await _get(session_factory, job_id)).status == JobStatus.failed
    assert (run.status, run.stop_reason) == (RunStatus.failed, StopReason.error)
    assert run.finished_at is not None and run.error and "gave up" in run.error
    assert [s.kind for s in steps] == ["error"]


async def test_final_failure_fails_its_run(session_factory: SessionFactory) -> None:
    run_id, job_id = await _run_for_job(session_factory)
    async with session_factory() as session:
        await queue.claim(session, "w1", 600)
        assert await queue.mark_failed(session, job_id, "boom", retry=False) == JobStatus.failed
    async with session_factory() as session:
        assert (await session.get_one(Run, run_id)).status == RunStatus.failed


async def test_only_the_lock_owner_can_finish_a_job(session_factory: SessionFactory) -> None:
    [job_id] = await _enqueue(session_factory)
    async with session_factory() as session:
        await queue.claim(session, "w1", 600)
        assert not await queue.heartbeat(session, job_id, "w2")
        assert not await queue.mark_done(session, job_id, owner="w2")
        assert await queue.mark_failed(session, job_id, "x", owner="w2") is None
        assert await queue.heartbeat(session, job_id, "w1")
        assert await queue.mark_done(session, job_id, owner="w1")
    assert (await _get(session_factory, job_id)).status == JobStatus.done


async def test_heartbeat_keeps_a_long_job_from_being_reclaimed(
    session_factory: SessionFactory,
) -> None:
    started = asyncio.Event()

    async def slow(_sf: SessionFactory, _payload: dict[str, Any]) -> None:
        started.set()
        await asyncio.sleep(2.5)

    worker = Worker(session_factory, {"slow": slow}, lock_timeout_seconds=1, heartbeat_seconds=0.2)
    async with session_factory() as session:
        job = await queue.enqueue(session, "slow", {})
        await session.commit()
    task = asyncio.create_task(worker.run_once())
    await started.wait()
    await asyncio.sleep(1.5)  # longer than the lock timeout
    async with session_factory() as session:
        assert await queue.claim(session, "other", 1) is None
    assert await task
    assert (await _get(session_factory, job.id)).status == JobStatus.done


async def test_lost_lock_cancels_the_job(session_factory: SessionFactory) -> None:
    started = asyncio.Event()
    finished = False

    async def slow(_sf: SessionFactory, _payload: dict[str, Any]) -> None:
        nonlocal finished
        started.set()
        await asyncio.sleep(5)
        finished = True

    worker = Worker(session_factory, {"slow": slow}, heartbeat_seconds=0.1)
    async with session_factory() as session:
        job = await queue.enqueue(session, "slow", {})
        await session.commit()
    task = asyncio.create_task(worker.run_once())
    await started.wait()
    async with session_factory() as session:  # another worker took it over
        await session.execute(update(Job).where(Job.id == job.id).values(locked_by="other"))
        await session.commit()
    await asyncio.wait_for(task, timeout=3)
    assert not finished
    stored = await _get(session_factory, job.id)
    assert stored.status == JobStatus.running and stored.locked_by == "other"


async def test_shutdown_releases_the_job_without_using_an_attempt(
    session_factory: SessionFactory,
) -> None:
    started = asyncio.Event()

    async def slow(_sf: SessionFactory, _payload: dict[str, Any]) -> None:
        started.set()
        await asyncio.sleep(10)

    worker = Worker(session_factory, {"slow": slow}, shutdown_grace_seconds=0.2)
    async with session_factory() as session:
        job = await queue.enqueue(session, "slow", {})
        await session.commit()
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    await started.wait()
    stop.set()
    await asyncio.wait_for(task, timeout=3)
    stored = await _get(session_factory, job.id)
    assert stored.status == JobStatus.queued and stored.attempts == 0
    assert stored.locked_by is None
