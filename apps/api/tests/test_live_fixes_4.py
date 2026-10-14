"""Live broken-env rerun (2026-10-14): incidents "checkout-worker: 5 error lines" and
"checkout-api: 6 error lines". The fixes worked, but the run looked like two failures:

1. The worker's incident failed ("No fix") and was then taken over by the api's: the list showed
   both as Not resolved.
2. The worker has no health check: every poll failed, yet its spike was not "down", so no
   emergency fix; and "No fix" came 30 s before the api's signal.
3. Verify kept "1 health-check failures reported by ECS" from the task the restart replaced
   (the follow-up fix judges each task by its own record; see the verify test).
4. The first search was the raw JSON log line; the follow-up brought SQL Server articles.
5. Memory found the earlier broken-env incident at 0.79 (threshold 0.85).
6. The secret rollback said "back to version terrafor".
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.actions.fake import FakeEmergencyActions, FakeSecret, FakeService
from app.agents.medic import fixes as fx
from app.agents.medic import memory as mem
from app.agents.medic.agent import NO_FIX_JOB
from app.agents.medic.events import ChangeMarker, Signal
from app.agents.medic.jobs import (
    investigate_incident_job,
    no_fix_deadline_job,
    rediagnose_incident_job,
)
from app.agents.medic.verify import health_failures, service_tasks, started_since
from app.db.models import Incident, Job, Run, RunStatus, RunStep, Workspace
from app.db.session import SessionFactory
from app.incidents import rules
from app.incidents.store import REDIAGNOSE_JOB, open_incident, set_status
from app.llm.fake import FakeLLM, reply
from app.tools.tavily import SearchResult, relevant, search_query, stack_terms, with_stack
from tests.test_api import Member, _member
from tests.test_incidents_api import client as api_client
from tests.test_medic import (
    SECRET_ARN,
    TD,
    Harness,
    _now,
    _state,
    demo_graph,
    eid,
    env_evidence,
    settings,
)
from tests.test_medic import use_deps as medic_deps

pytestmark = pytest.mark.anyio

use_deps = medic_deps
client = api_client

# The worker's line from /ecs/checkout-worker, as the evidence holds it (cut short).
POLL_FAILED = (
    '{"ts": "2026-10-14T06:49:04.183972+00:00", "level": "error", "service": "worker", '
    '"logger": "worker", "msg": "poll failed", "exc": "Traceback (most recent call last):\\n '
    'File \\"/app/app/main.py\\", line 75, in run\\n await poll_once(engine, settings.batch_'
    'size)\\n File \\"/app/app/main.py\\", line 38, in process_batch\\n async with engine.'
    "begin() as conn:\\n ^..."
)


@pytest.fixture
async def frank(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 6301, "frank-live4")


# --- 2. down without a health check, and the "No fix" grace --------------------------------------


T0 = datetime(2026, 10, 14, 6, 50, tzinfo=UTC)


def _logs(minutes: int, info_every: int | None = None) -> list[tuple[datetime, str]]:
    """A quiet hour, then one poll a minute for `minutes` minutes, every one failing (or an
    info line between failures when info_every is set)."""
    out = [
        (T0 - timedelta(minutes=60 - m), '{"level": "info", "msg": "poll ok"}') for m in range(50)
    ]
    for m in range(minutes):
        at = T0 - timedelta(minutes=minutes - m) + timedelta(seconds=5)
        out.append((at, '{"level": "error", "msg": "poll failed"}'))
        if info_every and m % info_every == 0:
            out.append((at + timedelta(seconds=10), '{"level": "info", "msg": "poll ok"}'))
    return out


def test_an_error_spike_where_every_poll_fails_is_down() -> None:
    signal = rules.log_signal(uuid.uuid4(), "checkout-worker", _logs(5), T0)
    assert signal is not None and signal.down
    assert "100% of its log lines are errors: nearly every operation fails" in signal.detail
    assert fx.is_down(signal)
    # Half the polls still work: an error spike, not down.
    partial = rules.log_signal(uuid.uuid4(), "checkout-worker", _logs(5, info_every=1), T0)
    assert partial is not None and not partial.down and not fx.is_down(partial)
    # Too short to call it down (under two minutes of failures).
    assert rules.error_share(_logs(5), T0 - timedelta(seconds=90), T0) is None


def _rc(ev: Any) -> str:
    secret_line = eid(ev, "secrets:checkout/database-url", "last changed")
    return json.dumps(
        {
            "summary": "checkout/database-url changed to an unreachable host.",
            "root_cause": "The DATABASE_URL secret changed; the services cannot connect.",
            "claims": [{"text": "Secret changed in the window.", "evidence_ids": [secret_line]}],
            "triggering_change": {
                "kind": "secret_change",
                "ref": f"{SECRET_ARN}:v-bad",
                "evidence_ids": [secret_line],
            },
            "affected_resources": ["checkout-worker"],
            "confidence": "high",
            "fix_options": [],
        }
    )


def _actions(sf: SessionFactory) -> FakeEmergencyActions:
    return FakeEmergencyActions(
        sf,
        services={
            "checkout/checkout-api": FakeService(revisions=[f"{TD}/checkout-api:17"]),
            "checkout/checkout-worker": FakeService(revisions=[f"{TD}/checkout-worker:41"]),
        },
        secrets={
            SECRET_ARN: FakeSecret(versions={"v-good": ["AWSPREVIOUS"], "v-bad": ["AWSCURRENT"]})
        },
    )


async def _open(
    sf: SessionFactory, ws: uuid.UUID, rid: uuid.UUID, rule: str, detail: str, down: bool = False
) -> tuple[uuid.UUID, uuid.UUID, bool]:
    signal = Signal(
        rule=rule,
        resource_id=rid,
        resource_name="svc",
        detail=detail,
        at=_now() - timedelta(minutes=2),
        down=down,
    )
    async with sf() as session:
        opened = await open_incident(
            session, workspace_id=ws, resource_ids=[rid], detected_by="watcher", signals=[signal]
        )
    return opened.incident.id, opened.run.id, opened.deduped


async def _jobs(sf: SessionFactory, kind: str) -> list[Job]:
    async with sf() as session:
        return list(await session.scalars(select(Job).where(Job.kind == kind)))


async def test_a_down_worker_gets_the_secret_rollback_without_a_health_check(
    session_factory: SessionFactory, workspace: Workspace, use_deps: Any
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    ev = env_evidence(ids, _now())
    actions = _actions(sf)
    use_deps(Harness(sf, ev, FakeLLM(reply(_rc(ev)), settings=settings()), actions=actions))
    incident_id, run_id, _ = await _open(
        sf, workspace.id, ids["worker"], "log_error_spike", "5 error lines; 100% errors", down=True
    )
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})
    incident, state, _ = await _state(sf, incident_id)
    assert incident.status == "waiting_approval"
    assert state.fixes[0].fix.action == "secret_rollback_version"


async def test_no_fix_waits_for_the_api_signal_then_rediagnoses_with_it(
    session_factory: SessionFactory, workspace: Workspace, use_deps: Any
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    ev = env_evidence(ids, _now())
    llm = FakeLLM(reply(_rc(ev)), reply(_rc(ev)), settings=settings())
    use_deps(Harness(sf, ev, llm, actions=_actions(sf)))
    # The worker's spike alone: not down (some polls still log info), no emergency fix.
    worker_id, run_id, _ = await _open(
        sf, workspace.id, ids["worker"], "log_error_spike", "5 lines"
    )
    await investigate_incident_job(sf, {"incident_id": str(worker_id), "run_id": str(run_id)})
    incident, state, run = await _state(sf, worker_id)
    assert incident.status == "investigating" and run.status not in (
        RunStatus.succeeded,
        RunStatus.failed,
    )
    assert state.fixes == [] and state.no_fix_until is not None
    (deadline,) = await _jobs(sf, NO_FIX_JOB)
    assert deadline.run_after is not None
    assert 85 <= (deadline.run_after - _now()).total_seconds() <= 95

    # 30 s later the api's health checks fail: it joins the worker's incident (no takeover).
    joined, _, deduped = await _open(
        sf, workspace.id, ids["api"], "health_check", "1 health-check failures"
    )
    assert deduped and joined == worker_id
    (job,) = await _jobs(sf, REDIAGNOSE_JOB)
    await rediagnose_incident_job(sf, job.payload)
    incident, state, _ = await _state(sf, worker_id)
    assert incident.status == "waiting_approval" and state.no_fix_until is None
    assert state.fixes[0].fix.action == "secret_rollback_version"

    # The grace deadline arrives afterwards: nothing changes.
    await no_fix_deadline_job(sf, deadline.payload)
    incident, _, _ = await _state(sf, worker_id)
    assert incident.status == "waiting_approval"


async def test_no_fix_fails_once_the_grace_is_over_without_new_signals(
    session_factory: SessionFactory, workspace: Workspace, use_deps: Any
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    ev = env_evidence(ids, _now())
    use_deps(Harness(sf, ev, FakeLLM(reply(_rc(ev)), settings=settings()), actions=_actions(sf)))
    worker_id, run_id, _ = await _open(
        sf, workspace.id, ids["worker"], "log_error_spike", "5 lines"
    )
    await investigate_incident_job(sf, {"incident_id": str(worker_id), "run_id": str(run_id)})
    (deadline,) = await _jobs(sf, NO_FIX_JOB)
    await no_fix_deadline_job(sf, deadline.payload)
    incident, state, run = await _state(sf, worker_id)
    assert incident.status == "failed" and run.status == RunStatus.succeeded
    assert state.outcome is not None and state.outcome.reason is not None
    assert state.outcome.reason.startswith("No fix Medic can apply.")
    async with sf() as session:
        kinds = [
            s.output_summary
            for s in await session.scalars(
                select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.seq)
            )
        ]
    assert "No fix yet; waiting 90 s for related signals" in kinds


# --- 1. merged incidents ----------------------------------------------------------------------


async def test_a_taken_over_incident_is_merged_linked_both_ways_and_hidden_by_default(
    session_factory: SessionFactory, client: httpx.AsyncClient, frank: Member
) -> None:
    sf = session_factory
    ws = frank.workspace_id
    ids = await demo_graph(sf, ws)
    worker_id, run_id, _ = await _open(sf, ws, ids["worker"], "log_error_spike", "5 lines")
    async with sf() as session:
        # Medic gave up on it ("No fix"), as in the live run.
        worker = await session.get_one(Incident, worker_id)
        run = await session.get_one(Run, run_id)
        await set_status(session, worker, run, "failed", reason="No fix")
        worker.window_end = datetime.now(UTC)
        run.status = RunStatus.succeeded
        await session.commit()
    api_id, _, deduped = await _open(sf, ws, ids["api"], "health_check", "1 health-check failures")
    assert not deduped and api_id != worker_id

    base: dict[str, Any] = {"workspace_id": str(ws)}
    listed = await client.get("/incidents", params=base, headers=frank.headers)
    assert [i["id"] for i in listed.json()["items"]] == [str(api_id)]
    merged = await client.get(
        "/incidents", params={**base, "status": "merged"}, headers=frank.headers
    )
    (row,) = merged.json()["items"]
    assert row["id"] == str(worker_id) and row["status"] == "merged"
    assert row["merged_into"] == str(api_id)
    api = (await client.get(f"/incidents/{api_id}", headers=frank.headers)).json()
    assert api["merged_from"] == [str(worker_id)]
    async with sf() as session:
        last = await session.scalar(
            select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.seq.desc())
        )
    assert last is not None and last.data is not None
    assert (last.data["status"], last.data["previous_status"]) == ("merged", "failed")
    assert last.data["merged_into"] == str(api_id)


# --- 3. verify counts only the current tasks' health failures ------------------------------------


class _Ecs:
    """ListTasks / DescribeTasks for the verify checks, recording what was described."""

    def __init__(self, running: list[dict[str, Any]], describable: list[dict[str, Any]]) -> None:
        self.running, self.describable = (
            running,
            {t["taskArn"].rsplit("/", 1)[-1]: t for t in describable},
        )
        self.described: list[str] = []

    def paginate(self, service: str, region: Any, op: str, key: str, **kw: Any) -> list[str]:
        assert (op, kw["desiredStatus"], kw["serviceName"]) == (
            "ListTasks",
            "RUNNING",
            "checkout-api",
        )
        return [t["taskArn"] for t in self.running]

    def call(self, service: str, region: Any, op: str, **kw: Any) -> dict[str, Any]:
        assert op == "DescribeTasks"
        self.described += kw["tasks"]
        return {"tasks": [self.describable[i] for i in kw["tasks"] if i in self.describable]}


def _task(task_id: str, started: datetime, **fields: Any) -> dict[str, Any]:
    return {
        "taskArn": f"arn:aws:ecs:us-east-2:1:task/checkout/{task_id}",
        "startedAt": started,
        **fields,
    }


def test_verify_judges_each_task_by_its_own_record_not_by_service_events() -> None:
    """Live broken-env, 2026-10-14 11:05: the fix (secret rollback + restart) landed at 11:05:03.
    Task a759e4db had started at 11:04:10 on the broken secret; ECS reported "(task a759e4db...)
    failed container health checks" at 11:05:56 and stopped it at 11:06:34. It was not on the one
    capped page of stopped tasks verify read, so for 14 rounds the event counted as "1
    health-check failures reported by ECS" while the fix's task 602e5a1d was HEALTHY."""
    fix = datetime(2026, 10, 14, 11, 5, 3, tzinfo=UTC)
    old, new = "a759e4dbd308403387eb0e99dc45cd4a", "602e5a1d53d745dca0fa28eef089a8bf"
    service = {
        "serviceName": "checkout-api",
        "deployments": [{"status": "PRIMARY", "createdAt": fix + timedelta(seconds=1)}],
        "events": [
            {
                "createdAt": fix + timedelta(seconds=53),
                "message": f"(service checkout-api) (task {old}) failed container health checks.",
            },
        ],
    }
    replaced = _task(
        old,
        fix - timedelta(seconds=53),
        lastStatus="STOPPED",
        desiredStatus="STOPPED",
        healthStatus="UNHEALTHY",
        stoppedAt=fix + timedelta(seconds=91),
        stoppedReason="Task failed container health checks",
        containers=[{"exitCode": 137}],
    )
    fixed = _task(
        new, fix + timedelta(seconds=37), lastStatus="RUNNING", desiredStatus="RUNNING",
        healthStatus="HEALTHY",
    )  # fmt: skip
    ecs = _Ecs(running=[fixed], describable=[fixed, replaced])
    tasks = service_tasks(ecs, "us-east-2", "checkout", service, [], fix)  # type: ignore[arg-type]
    assert old in ecs.described  # the event only points at the task to read
    assert {t["taskArn"].rsplit("/", 1)[-1] for t in tasks} == {old, new}
    assert health_failures(tasks, fix) == ([], [])
    assert not started_since(replaced, fix) and started_since(fixed, fix)

    # The fix's own task failing its checks counts, stopped or still running.
    sick = {**fixed, "healthStatus": "UNHEALTHY"}
    assert health_failures([replaced, sick], fix) == ([], [sick])
    stopped_new = {
        **fixed,
        "lastStatus": "STOPPED",
        "desiredStatus": "STOPPED",
        "stoppedReason": "Task failed container health checks",
    }
    assert health_failures([replaced, stopped_new], fix) == ([stopped_new], [])
    # A task being drained that turns unhealthy on its way out does not.
    draining = {**sick, "desiredStatus": "STOPPED"}
    assert health_failures([draining], fix) == ([], [])


# --- 4. research queries and results ------------------------------------------------------------


def test_a_cut_short_json_line_searches_its_message_not_the_json() -> None:
    assert search_query(POLL_FAILED) == "poll failed"
    whole = (
        '{"level": "error", "msg": "poll failed", "exc": "Traceback...\\nasyncpg.exceptions.'
        "ConnectionDoesNotExistError: connection was closed"
    )
    assert search_query(whole).startswith(
        "poll failed asyncpg.exceptions.ConnectionDoesNotExistError"
    )


def test_follow_up_queries_carry_the_stack_and_drop_other_databases() -> None:
    stack = stack_terms(
        [
            'File "/app/.venv/lib/sqlalchemy/engine/base.py"',
            "asyncpg connect failed",
            "rds:db postgres",
        ]
    )
    assert stack == ["postgres", "asyncpg", "sqlalchemy"]
    query = with_stack("sqlalchemy engine.begin connection failed invalid database url", stack)
    assert query.endswith("postgres asyncpg")
    results = [
        SearchResult(
            title="SQL Server connection string is invalid",
            url="https://learn.microsoft.com/sql-server/connect",
            snippet="SQL Server: invalid connection string for the database.",
        ),
        SearchResult(
            title="asyncpg: connection refused after changing DATABASE_URL",
            url="https://example.com/asyncpg",
            snippet="SQLAlchemy with asyncpg cannot connect to Postgres.",
        ),
        SearchResult(title="Holiday recipes", url="https://example.com/food", snippet="Bake it."),
    ]
    assert [r.url for r in relevant(results, query, stack)] == ["https://example.com/asyncpg"]


# --- 5. memory: the same secret changed before the same symptoms ---------------------------------


async def test_the_same_secret_change_on_the_same_services_is_remembered(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    old_ref = "arn:aws:secretsmanager:us-east-2:881050425883:secret:checkout/database-url-Hk6C1T"
    async with sf() as session:
        past = Incident(
            workspace_id=workspace.id,
            title="checkout-api: health checks failing",
            status="resolved",
            resolved_at=_now() - timedelta(days=1),
            remembered_at=_now() - timedelta(days=1),
            affected_resource_ids=[ids["api"], ids["worker"]],
            signals=[
                {"rule": r, "resource_id": str(ids["api"])}
                for r in ("health_check", "log_error_spike", "ecs_deployment_failed")
            ],
            state={
                "root_cause": {
                    "summary": "checkout/database-url changed",
                    "triggering_change": {"kind": "secret_change", "ref": f"{old_ref}:e6550203"},
                }
            },
        )
        now = Incident(
            workspace_id=workspace.id,
            title="checkout-worker: 5 error lines",
            affected_resource_ids=[ids["worker"]],
            signals=[{"rule": "log_error_spike", "resource_id": str(ids["worker"])}],
        )
        session.add_all([past, now])
        await session.flush()
        change = ChangeMarker(
            kind="secret_change", ref=f"{old_ref}:f99e4703", at=_now(), label="changed"
        )
        (hit,) = await mem.similar(session, now, None, [change])
        assert hit.similarity >= 0.85  # the live pair scored 0.79 before
        (without,) = await mem.similar(session, now, None, [])
        assert without.similarity < 0.85
    assert mem.change_target("secret_change", f"{old_ref}:x") == "secret:checkout/database-url"
    assert mem.change_target("commit", "abc1234") is None
