"""Incident detection (Phase 5 M2): detector rules on scenario-shaped API responses, the incident
store's dedupe, the watcher pass, the watch job chain, and POST /incidents."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from app.agents.medic.events import Signal
from app.config import get_settings
from app.connectors.aws import AwsReader
from app.db.models import (
    Connection,
    Edge,
    Incident,
    Job,
    JobStatus,
    Resource,
    Run,
    RunStatus,
    RunStep,
    StopReason,
    Workspace,
)
from app.db.session import SessionFactory, get_session_factory
from app.incidents import rules
from app.incidents.store import (
    INVESTIGATE_JOB,
    ResourceNotFoundError,
    open_incident,
    set_status,
)
from app.incidents.watcher import WATCH_JOB, ensure_watch_job, watch_once
from app.main import app
from tests.aws_fixture import ACCOUNT, REGION, fixture_session
from tests.test_api import Member, _member, _settings

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)
CLUSTER = f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/checkout"
OOM = "OutOfMemoryError: Container killed due to memory usage"


def ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


def service(
    name: str, *, running: int = 0, rollout: str = "FAILED", events: Any = ()
) -> dict[str, Any]:
    return {
        "serviceArn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/checkout/{name}",
        "serviceName": name,
        "clusterArn": CLUSTER,
        "status": "ACTIVE",
        "runningCount": running,
        "desiredCount": 1,
        "events": [{"createdAt": at, "message": m} for at, m in events],
        "deployments": [
            {
                "id": "ecs-svc/2",
                "status": "PRIMARY",
                "taskDefinition": f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{name}:42",
                "rolloutState": rollout,
                "rolloutStateReason": "ECS deployment circuit breaker: tasks failed to start.",
                "updatedAt": ago(2),
            }
        ],
    }


def task(minutes: float, reason: str, code: int | None) -> dict[str, Any]:
    return {
        "taskArn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/checkout/{uuid.uuid4().hex}",
        "stoppedAt": ago(minutes),
        "stoppedReason": reason,
        "containers": [{"name": "app", "exitCode": code}],
    }


# --- rules ------------------------------------------------------------------------------------


def test_oom_kills_and_the_failed_rollout_are_signals() -> None:
    rid = uuid.uuid4()
    tasks = [task(m, OOM, 137) for m in (1, 3, 4)] + [task(20, OOM, 137)]
    found = rules.ecs_signals(rid, service("checkout-worker"), tasks, NOW)
    by_rule = {s.rule: s for s in found}
    assert set(by_rule) == {"ecs_deployment_failed", "ecs_tasks_stopped"}
    stopped = by_rule["ecs_tasks_stopped"]
    assert stopped.detail.startswith("3 tasks stopped with exit code 137 (3 OutOfMemoryError)")
    assert stopped.at == ago(4) and stopped.resource_id == rid


def test_failed_health_checks_are_their_own_signal_not_a_crash() -> None:
    tasks = [
        task(4, "Simulated Fargate task retirement", 0),
        task(3, "Task failed container health checks", 1),
        task(1, "Task failed container health checks", 1),
    ]
    found = rules.ecs_signals(
        uuid.uuid4(), service("checkout-api", rollout="IN_PROGRESS"), tasks, NOW
    )
    assert [s.rule for s in found] == ["health_check"]
    assert found[0].detail == (
        "2 tasks stopped failing health checks (0/1 tasks running, not healthy)"
    )


def test_a_healthy_service_gives_no_signal() -> None:
    healthy = service("checkout-api", running=1, rollout="COMPLETED")
    assert rules.ecs_signals(uuid.uuid4(), healthy, [task(2, "Scaling activity", 0)], NOW) == []


def _events(per_minute_before: float, recent: int) -> list[tuple[datetime, str]]:
    events = [(ago(29 - i), "poll ok") for i in range(25)]
    count = int(per_minute_before * 25)
    events += [(ago(29 - i * 25 / max(count, 1)), "ERROR poll failed") for i in range(count)]
    events += [
        (ago(4 - i * 4 / max(recent, 1)), "poll failed: could not translate host name")
        for i in range(recent)
    ]
    return events


def test_log_error_spike_needs_volume_and_a_jump() -> None:
    assert rules.log_signal(uuid.uuid4(), "worker", _events(0, 3), NOW) is None
    assert rules.log_signal(uuid.uuid4(), "worker", _events(2, 12), NOW) is None
    spike = rules.log_signal(uuid.uuid4(), "worker", _events(0.1, 24), NOW)
    assert spike is not None and spike.detail.startswith("24 error lines in the last 5 minutes")


def _series(before: float, after: float) -> list[tuple[datetime, float]]:
    return [(ago(m), before if m > 5 else after) for m in range(29, -1, -1)]


def test_rds_needs_cpu_and_latency_together() -> None:
    rid = uuid.uuid4()
    hot = rules.rds_signal(rid, "db", _series(5, 85), _series(0.8, 25), NOW)
    assert hot is not None and hot.rule == "rds_cpu_latency"
    assert hot.detail == "CPU 85% (baseline 5%), read latency 25.0 ms (baseline 0.8 ms)"
    assert rules.rds_signal(rid, "db", _series(5, 85), _series(0.8, 0.9), NOW) is None
    assert rules.rds_signal(rid, "db", _series(30, 45), _series(0.8, 25), NOW) is None


# --- graph helpers ----------------------------------------------------------------------------


async def _demo(sf: SessionFactory, ws: uuid.UUID) -> dict[str, uuid.UUID]:
    async with sf() as session:
        conn = Connection(
            workspace_id=ws,
            kind="aws",
            name="demo",
            status="connected",
            config={"account_id": ACCOUNT},
        )
        session.add(conn)
        await session.flush()

        def res(type_: str, name: str, **config: Any) -> Resource:
            r = Resource(
                workspace_id=ws,
                connection_id=conn.id,
                provider="aws",
                type=type_,
                name=name,
                external_id=f"{type_}:{name}",
                region=REGION,
                config=config,
            )
            session.add(r)
            return r

        api = res("ecs:service", "checkout-api", cluster_arn=CLUSTER)
        worker = res("ecs:service", "checkout-worker", cluster_arn=CLUSTER)
        db = res("rds:db", "checkout-orders-db")
        logs = res("logs:log-group", "/ecs/checkout-worker")
        lonely = res("ecs:service", "lonely")
        await session.flush()
        session.add_all(
            [
                Edge(workspace_id=ws, from_id=api.id, to_id=db.id, kind="reads"),
                Edge(workspace_id=ws, from_id=worker.id, to_id=db.id, kind="reads"),
                Edge(workspace_id=ws, from_id=worker.id, to_id=logs.id, kind="logs_to"),
            ]
        )
        await session.commit()
        return {
            "api": api.id,
            "worker": worker.id,
            "db": db.id,
            "logs": logs.id,
            "lonely": lonely.id,
            "conn": conn.id,
        }


def _signal(rid: uuid.UUID, rule: str = "ecs_tasks_stopped", minutes: float = 2) -> Signal:
    return Signal(rule=rule, resource_id=rid, resource_name="x", detail="d", at=ago(minutes))


async def _count(sf: SessionFactory, model: Any, *where: Any) -> int:
    async with sf() as session:
        return int(await session.scalar(select(func.count(model.id)).where(*where)) or 0)


# --- store ------------------------------------------------------------------------------------


async def test_open_incident_creates_the_incident_its_run_and_the_job(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    ids = await _demo(session_factory, workspace.id)
    async with session_factory() as session:
        opened = await open_incident(
            session,
            workspace_id=workspace.id,
            resource_ids=[ids["worker"]],
            detected_by="watcher",
            signals=[_signal(ids["worker"])],
            now=NOW,
        )
    assert not opened.deduped
    async with session_factory() as session:
        incident = await session.get(Incident, opened.incident.id)
        run = await session.get(Run, opened.run.id)
        assert incident is not None and run is not None
        assert incident.status == "open" and incident.severity == "high"
        assert incident.detector_rule == "ecs_tasks_stopped" and incident.detected_by == "watcher"
        assert incident.window_start == ago(32) and incident.first_signal_at == ago(2)
        # The worker's group: itself, its database, its log group.
        assert set(incident.group_resource_ids) == {ids["worker"], ids["db"], ids["logs"]}
        assert (
            run.agent == "medic"
            and run.incident_id == incident.id
            and run.status == RunStatus.queued
        )
        steps = list(await session.scalars(select(RunStep).where(RunStep.run_id == run.id)))
        assert [s.kind for s in steps] == ["incident_opened"]
        assert steps[0].data is not None and steps[0].data["detector_rule"] == "ecs_tasks_stopped"
        job = await session.scalar(select(Job).where(Job.kind == INVESTIGATE_JOB))
        assert job is not None and job.payload == {
            "incident_id": str(incident.id),
            "run_id": str(run.id),
        }


async def test_related_resources_join_the_open_incident(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    ids = await _demo(session_factory, workspace.id)
    async with session_factory() as session:
        first = await open_incident(
            session,
            workspace_id=workspace.id,
            resource_ids=[ids["worker"]],
            detected_by="watcher",
            signals=[_signal(ids["worker"])],
            now=NOW,
        )
    async with session_factory() as session:
        # The database is in the worker incident's group: a report on it joins.
        second = await open_incident(
            session,
            workspace_id=workspace.id,
            resource_ids=[ids["db"]],
            detected_by="user",
            signals=[],
            description="search is slow",
            now=NOW,
        )
    async with session_factory() as session:
        third = await open_incident(
            session,
            workspace_id=workspace.id,
            resource_ids=[ids["lonely"]],
            detected_by="user",
            signals=[],
            now=NOW,
        )
    assert second.deduped and second.incident.id == first.incident.id
    assert second.run.id == first.run.id
    assert not third.deduped and third.incident.id != first.incident.id
    async with session_factory() as session:
        joined = await session.get(Incident, first.incident.id)
        assert joined is not None
        assert [s["rule"] for s in joined.signals] == ["ecs_tasks_stopped", "user_report"]
        assert joined.affected_resource_ids == [ids["worker"], ids["db"]]
        assert ids["api"] in joined.group_resource_ids  # the db's other reader


async def test_services_sharing_a_database_join_whichever_comes_first(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    ids = await _demo(session_factory, workspace.id)
    async with session_factory() as session:
        api = await open_incident(
            session,
            workspace_id=workspace.id,
            resource_ids=[ids["api"]],
            detected_by="watcher",
            signals=[_signal(ids["api"], "health_check")],
            now=NOW,
        )
    async with session_factory() as session:
        # The api's group is {api, db}; the worker is not in it, but its group shares the db.
        worker = await open_incident(
            session,
            workspace_id=workspace.id,
            resource_ids=[ids["worker"]],
            detected_by="watcher",
            signals=[_signal(ids["worker"])],
            now=NOW,
        )
    assert worker.deduped and worker.incident.id == api.incident.id


async def test_a_closed_incident_does_not_absorb_new_reports(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    ids = await _demo(session_factory, workspace.id)
    async with session_factory() as session:
        first = await open_incident(
            session,
            workspace_id=workspace.id,
            resource_ids=[ids["api"]],
            detected_by="user",
            signals=[],
            now=NOW,
        )
        incident = await session.get(Incident, first.incident.id)
        run = await session.get(Run, first.run.id)
        assert incident is not None and run is not None
        await set_status(session, incident, run, "resolved", reason="healthy", now=NOW)
        await session.commit()
        assert incident.resolved_at == NOW and incident.window_end == NOW
    async with session_factory() as session:
        again = await open_incident(
            session,
            workspace_id=workspace.id,
            resource_ids=[ids["api"]],
            detected_by="user",
            signals=[],
            now=NOW,
        )
    assert not again.deduped
    async with session_factory() as session:
        steps = list(
            await session.scalars(
                select(RunStep).where(RunStep.run_id == first.run.id).order_by(RunStep.seq)
            )
        )
        assert [s.kind for s in steps] == ["incident_opened", "incident_updated"]
        assert steps[1].data == {
            "type": "incident_updated",
            "incident_id": str(first.incident.id),
            "status": "resolved",
            "previous_status": "open",
            "at": NOW.isoformat().replace("+00:00", "Z"),
            "reason": "healthy",
            "merged_into": None,
        }


async def test_unknown_resources_are_refused(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    async with session_factory() as session:
        with pytest.raises(ResourceNotFoundError):
            await open_incident(
                session,
                workspace_id=workspace.id,
                resource_ids=[uuid.uuid4()],
                detected_by="user",
                signals=[],
            )


async def test_concurrent_reports_open_one_incident(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    ids = await _demo(session_factory, workspace.id)

    async def report(rid: uuid.UUID) -> uuid.UUID:
        async with session_factory() as session:
            opened = await open_incident(
                session,
                workspace_id=workspace.id,
                resource_ids=[rid],
                detected_by="watcher",
                signals=[_signal(rid)],
                now=NOW,
            )
            return opened.incident.id

    results = await asyncio.gather(*(report(r) for r in (ids["api"], ids["db"], ids["worker"])))
    assert len(set(results)) == 1
    assert await _count(session_factory, Incident, Incident.workspace_id == workspace.id) == 1


# --- watcher ----------------------------------------------------------------------------------


def _aws(
    api_tasks: list[dict[str, Any]], worker_tasks: list[dict[str, Any]], *, rds_hot: bool
) -> dict[str, Any]:
    services = {
        "checkout-api": service("checkout-api", running=1, rollout="COMPLETED"),
        "checkout-worker": service("checkout-worker"),
        "lonely": service("lonely", running=1, rollout="COMPLETED"),
    }
    tasks = {"checkout-api": api_tasks, "checkout-worker": worker_tasks, "lonely": []}
    minutes = list(range(29, -1, -1))
    stamps = [ago(m) for m in minutes]
    cpu = [5.0 if m > 5 else (85.0 if rds_hot else 6.0) for m in minutes]
    lat = [0.0008 if m > 5 else (0.025 if rds_hot else 0.0009) for m in minutes]
    by_arn: dict[str, dict[str, Any]] = {}
    for ts in tasks.values():
        for t in ts:
            by_arn[t["taskArn"]] = t
    return {
        "ecs:DescribeServices": lambda p: {"services": [services[p["services"][0]]]},
        "ecs:ListTasks": lambda p: {"taskArns": [t["taskArn"] for t in tasks[p["serviceName"]]]},
        "ecs:DescribeTasks": lambda p: {"tasks": [by_arn[a] for a in p["tasks"]]},
        "logs:FilterLogEvents": {
            "events": [
                {"timestamp": int(ago(4 - i / 10).timestamp() * 1000), "message": "worker starting"}
                for i in range(3)
            ]
        },
        "cloudwatch:GetMetricData": {
            "MetricDataResults": [
                {"Id": "cpu", "Timestamps": stamps, "Values": cpu},
                {"Id": "lat", "Timestamps": stamps, "Values": lat},
            ]
        },
    }


async def test_one_pass_opens_one_incident_per_related_group(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    ids = await _demo(session_factory, workspace.id)
    calls: list[str] = []
    responses = _aws([], [task(m, OOM, 137) for m in (1, 3)], rds_hot=True)

    def reader_for(_: Connection) -> AwsReader:
        return AwsReader(fixture_session(responses, calls), get_settings())

    opened = await watch_once(session_factory, reader_for=reader_for, now=NOW)
    # The worker's OOM kills and the database's CPU spike share a group: one incident.
    assert len(opened) == 1 and not opened[0].deduped
    async with session_factory() as session:
        incident = await session.get(Incident, opened[0].incident.id)
        assert incident is not None
        assert set(incident.affected_resource_ids) == {ids["worker"], ids["db"]}
        assert {s["rule"] for s in incident.signals} == {
            "ecs_tasks_stopped",
            "ecs_deployment_failed",
            "rds_cpu_latency",
        }
        # The earliest signal names the rule: the CPU spike from -5 min, before the first kill.
        assert incident.detector_rule == "rds_cpu_latency"
    assert all(c.split(":", 1)[1].startswith(("Describe", "List", "Filter", "Get")) for c in calls)

    # The next pass sees the same signals: they join the open incident.
    again = await watch_once(session_factory, reader_for=reader_for, now=NOW)
    assert [o.deduped for o in again] == [True]
    assert await _count(session_factory, Incident, Incident.workspace_id == workspace.id) == 1

    # Once it is resolved, the same old stopped tasks do not reopen it.
    async with session_factory() as session:
        incident = await session.get(Incident, opened[0].incident.id)
        run = await session.get(Run, opened[0].run.id)
        assert incident is not None and run is not None
        await set_status(session, incident, run, "resolved", now=NOW)
        await session.commit()
    assert await watch_once(session_factory, reader_for=reader_for, now=NOW) == []


async def test_a_failing_check_is_skipped_not_an_incident(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    await _demo(session_factory, workspace.id)

    def broken(_: Connection) -> AwsReader:
        raise RuntimeError("AccessDenied")

    assert await watch_once(session_factory, reader_for=broken, now=NOW) == []


async def test_only_one_watch_job_is_queued(session_factory: SessionFactory) -> None:
    assert await ensure_watch_job(session_factory)
    assert not await ensure_watch_job(session_factory)
    assert (
        await _count(session_factory, Job, Job.kind == WATCH_JOB, Job.status == JobStatus.queued)
        == 1
    )


# --- POST /incidents --------------------------------------------------------------------------


@pytest.fixture
async def client(session_factory: SessionFactory) -> AsyncIterator[httpx.AsyncClient]:
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = lambda: _settings()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.dependency_overrides.clear()


async def test_report_incident_opens_then_dedupes(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    dana: Member = await _member(session_factory, 5001, "dana")
    erin: Member = await _member(session_factory, 5002, "erin")
    ids = await _demo(session_factory, dana.workspace_id)
    body = {
        "workspace_id": str(dana.workspace_id),
        "resource_ids": [str(ids["api"])],
        "description": "Orders stay pending",
        "severity": "critical",
    }
    first = await client.post("/incidents", json=body, headers=dana.headers)
    assert first.status_code == 202, first.text
    assert first.json()["deduped"] is False
    second = await client.post("/incidents", json=body, headers=dana.headers)
    assert second.json() == {**first.json(), "deduped": True}
    async with session_factory() as session:
        incident = await session.get(Incident, uuid.UUID(first.json()["incident_id"]))
        assert incident is not None and incident.severity == "critical"
        assert incident.title == "Reported problem on checkout-api"
        assert incident.summary == "Orders stay pending"
    # Another user's workspace, and a resource from another workspace.
    assert (await client.post("/incidents", json=body, headers=erin.headers)).status_code == 404
    foreign = {**body, "workspace_id": str(erin.workspace_id)}
    assert (await client.post("/incidents", json=foreign, headers=erin.headers)).status_code == 404


async def _failed(
    sf: SessionFactory, ws: uuid.UUID, rid: uuid.UUID, rule: str, at: datetime, **run: Any
) -> uuid.UUID:
    async with sf() as session:
        opened = await open_incident(
            session,
            workspace_id=ws,
            resource_ids=[rid],
            detected_by="watcher",
            signals=[_signal(rid, rule, minutes=(NOW - at).total_seconds() / 60)],
            now=at,
        )
        incident = await session.get(Incident, opened.incident.id)
        found = await session.get(Run, opened.run.id)
        assert incident is not None and found is not None
        await set_status(session, incident, found, "failed", reason="No fix", now=at)
        for key, value in run.items():
            setattr(found, key, value)
        await session.commit()
        return incident.id


async def test_a_related_outage_takes_over_the_incident_medic_just_failed(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    ids = await _demo(session_factory, workspace.id)
    # broken-env run #2: the worker's error spike failed ("No fix"), then the api's health
    # checks failed a minute later. Both read the database whose secret changed.
    worker = await _failed(session_factory, workspace.id, ids["worker"], "log_error_spike", ago(2))
    async with session_factory() as session:
        api = await open_incident(
            session,
            workspace_id=workspace.id,
            resource_ids=[ids["api"]],
            detected_by="watcher",
            signals=[_signal(ids["api"], "health_check", minutes=1)],
            now=NOW,
        )
    assert not api.deduped and api.incident.id != worker
    async with session_factory() as session:
        incident = await session.get(Incident, api.incident.id)
        assert incident is not None
        assert incident.affected_resource_ids == [ids["api"], ids["worker"]]
        assert [s["rule"] for s in incident.signals] == ["log_error_spike", "health_check"]
        assert incident.first_signal_at == ago(2)
        opened = await session.scalar(
            select(RunStep).where(RunStep.run_id == api.run.id, RunStep.kind == "incident_opened")
        )
        assert opened is not None and opened.data is not None
        assert opened.data["merged_from"] == [str(worker)]
        assert {r["name"] for r in opened.data["affected_resources"]} == {
            "checkout-api",
            "checkout-worker",
        }
        old_run = await session.scalar(select(Run).where(Run.incident_id == worker))
        assert old_run is not None
        last = await session.scalar(
            select(RunStep).where(RunStep.run_id == old_run.id).order_by(RunStep.seq.desc())
        )
        assert last is not None and last.data is not None
        assert last.data["merged_into"] == str(api.incident.id)


async def test_a_stopped_or_old_failed_incident_is_not_taken_over(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    ids = await _demo(session_factory, workspace.id)
    await _failed(
        session_factory,
        workspace.id,
        ids["worker"],
        "log_error_spike",
        ago(2),
        stop_reason=StopReason.stopped_by_user,
    )
    await _failed(session_factory, workspace.id, ids["worker"], "log_error_spike", ago(45))
    async with session_factory() as session:
        api = await open_incident(
            session,
            workspace_id=workspace.id,
            resource_ids=[ids["api"]],
            detected_by="watcher",
            signals=[_signal(ids["api"], "health_check", minutes=1)],
            now=NOW,
        )
        incident = await session.get(Incident, api.incident.id)
        assert incident is not None and incident.affected_resource_ids == [ids["api"]]
