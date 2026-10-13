"""Live missing-index run, round 3 (2026-10-13): incident "checkout-api: 8 error lines".

- Every deploy opened an error spike: uvicorn logs startup and shutdown under the logger
  "uvicorn.error" at INFO, and lines were matched on the word "error".
- Medic called it a false alarm while /orders/search returned 500s after 16-20 s statement
  timeouts; bigger spikes (52, then 306 error lines) arrived while its fix waited for approval.
- The fix PR restored the index but was titled "revert deployment that triggered false error
  spike".
- Verify showed a red "Failed" block every few seconds while the rollout ran, every round twice.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.actions.fake import FakeEmergencyActions
from app.agents.medic import agent
from app.agents.medic import fixes as fx
from app.agents.medic.events import ChangeMarker, Signal
from app.agents.medic.jobs import (
    investigate_incident_job,
    rediagnose_incident_job,
    verify_incident_job,
)
from app.agents.medic.verify import rollout_wait
from app.db.models import Approval, ApprovalStatus, Job, Workspace
from app.db.session import SessionFactory
from app.incidents import rules
from app.incidents.store import REDIAGNOSE_JOB, material_signals, open_incident
from app.llm.fake import FakeLLM, reply
from app.policy.engine import Decision
from app.telemetry.log_levels import is_error_line
from app.tools.tavily import dominant_error
from tests.test_medic import (
    REPO,
    Harness,
    _decide,
    _evidence,
    _now,
    _state,
    demo_graph,
    eid,
    github_routes,
    healthy_aws,
    settings,
    use_deps,  # noqa: F401  (pytest fixture)
)

pytestmark = pytest.mark.anyio

# Exact lines from /ecs/checkout-api on 2026-10-13 (CloudWatch, filter "uvicorn").
UVICORN_INFO = [
    '{"ts": "2026-10-13T06:34:01.695581+00:00", "level": "info", "service": "api", "logger": '
    '"uvicorn.error", "msg": "Started server process [1]", "color_message": "Started server '
    'process [\\u001b[36m%d\\u001b[0m]"}',
    '{"ts": "2026-10-13T06:34:01.695766+00:00", "level": "info", "service": "api", "logger": '
    '"uvicorn.error", "msg": "Waiting for application startup."}',
    '{"ts": "2026-10-13T06:34:01.700279+00:00", "level": "info", "service": "api", "logger": '
    '"uvicorn.error", "msg": "Application startup complete."}',
    '{"ts": "2026-10-13T06:34:01.743966+00:00", "level": "info", "service": "api", "logger": '
    '"uvicorn.error", "msg": "Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)", '
    '"color_message": "Uvicorn running on \\u001b[1m%s://%s:%d\\u001b[0m (Press CTRL+C to quit)"}',
    '{"ts": "2026-10-13T08:07:45.328648+00:00", "level": "info", "service": "api", "logger": '
    '"uvicorn.error", "msg": "Shutting down"}',
    '{"ts": "2026-10-13T08:07:45.429664+00:00", "level": "info", "service": "api", "logger": '
    '"uvicorn.error", "msg": "Waiting for application shutdown."}',
    '{"ts": "2026-10-13T08:07:45.435035+00:00", "level": "info", "service": "api", "logger": '
    '"uvicorn.error", "msg": "Application shutdown complete."}',
    '{"ts": "2026-10-13T08:07:45.435143+00:00", "level": "info", "service": "api", "logger": '
    '"uvicorn.error", "msg": "Finished server process [1]", "color_message": "Finished server '
    'process [\\u001b[36m%d\\u001b[0m]"}',
    '{"ts": "2026-10-13T10:39:50.220144+00:00", "level": "info", "service": "api", "logger": '
    '"uvicorn.error", "msg": "Waiting for connections to close. (CTRL+C to force quit)"}',
]
UVICORN_ERROR = (
    '{"ts": "2026-10-13T08:54:10.160350+00:00", "level": "error", "service": "api", "logger": '
    '"uvicorn.error", "msg": "Exception in ASGI application\\n", "exc": "Traceback (most recent '
    'call last):\\n  File \\"/opt/venv/lib/python3.12/site-packages/uvicorn/protocols/http/'
    'httptools_impl.py\\", line 422, in run_asgi\\nsqlalchemy.exc.DBAPIError: canceling '
    'statement due to statement timeout"}'
)


def _timeout_line(n: int) -> str:
    return json.dumps(
        {
            "level": "error",
            "logger": "app",
            "msg": "order search failed",
            "path": "/orders/search",
            "status": 500,
            "duration_ms": 16000 + n,
            "error": "asyncpg.exceptions.QueryCanceledError: canceling statement due to "
            "statement timeout",
        }
    )


# --- B1: level, not logger name ---------------------------------------------------------------


def test_uvicorn_info_lines_are_not_errors_but_its_exceptions_are() -> None:
    assert [is_error_line(x) for x in UVICORN_INFO] == [False] * len(UVICORN_INFO)
    assert is_error_line(UVICORN_ERROR)
    # Plain-text uvicorn and access logs: the level decides, a 5xx is an error.
    assert not is_error_line("INFO:     Started server process [1]")
    assert not is_error_line("INFO:     Waiting for application shutdown.")
    assert is_error_line("ERROR:    Exception in ASGI application")
    assert is_error_line('INFO:     10.0.1.5:4242 - "GET /orders/search HTTP/1.1" 500')
    assert not is_error_line('INFO:     10.0.1.5:4242 - "GET /orders/search HTTP/1.1" 200')
    assert not is_error_line('time=2026-10-13T10:39:50Z level=info msg="uvicorn.error ready"')
    # No level at all: words still count, but not inside a dotted logger name.
    assert is_error_line("connection refused by db:5432")
    assert not is_error_line("uvicorn.error: Started server process [1]")
    assert is_error_line(_timeout_line(1))


def test_a_deploy_restart_is_not_an_error_spike() -> None:
    now = datetime(2026, 10, 13, 11, 18, tzinfo=UTC)
    restart = [(now - timedelta(seconds=60 - i), line) for i, line in enumerate(UVICORN_INFO * 2)]
    baseline = [(now - timedelta(minutes=20), '{"level": "info", "msg": "GET /orders 200"}')]
    assert rules.log_signal(uuid.uuid4(), "checkout-api", baseline + restart, now) is None
    real = [(now - timedelta(seconds=30 + i), _timeout_line(i)) for i in range(8)]
    spike = rules.log_signal(uuid.uuid4(), "checkout-api", baseline + restart + real, now)
    assert spike is not None and spike.detail.startswith("8 error lines")
    # Research searches the real error, never uvicorn's startup.
    assert "statement timeout" in dominant_error([*UVICORN_INFO, *map(_timeout_line, range(3))])


# --- B2: new signals and "false alarm" ----------------------------------------------------------


def _sig(rule: str, detail: str, minutes: float = 1.0) -> dict[str, Any]:
    return Signal(
        rule=rule,
        resource_id=uuid.uuid4(),
        resource_name="checkout-api",
        detail=detail,
        at=datetime(2026, 10, 13, 11, 17, tzinfo=UTC) + timedelta(minutes=minutes),
    ).model_dump(mode="json")


def test_material_signals_are_new_rules_or_much_bigger_spikes() -> None:
    first = [_sig("log_error_spike", "8 error lines in the last 5 minutes")]
    assert material_signals(first, [_sig("log_error_spike", "12 error lines in the last 5")]) == []
    bigger = _sig("log_error_spike", "112 error lines in the last 5 minutes")
    assert material_signals(first, [bigger]) == [bigger]
    slow = _sig("rds_cpu_latency", "p50 13860 ms, CPU 56%")
    assert material_signals(first, [slow]) == [slow]


async def test_identical_signals_are_kept_once_and_new_evidence_queues_a_rediagnosis(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    at = _now() - timedelta(minutes=2)

    def signal(detail: str, rule: str = "log_error_spike") -> Signal:
        return Signal(
            rule=rule,
            resource_id=ids["api"],
            resource_name="checkout-api",
            detail=detail,
            at=at,
        )

    async with sf() as session:
        opened = await open_incident(
            session,
            workspace_id=workspace.id,
            resource_ids=[ids["api"]],
            detected_by="watcher",
            signals=[signal("8 error lines in the last 5 minutes")],
        )
        opened.incident.status = "waiting_approval"
        await session.commit()
    for detail in ["8 error lines in the last 5 minutes"] * 3 + ["306 error lines in 5 min"]:
        async with sf() as session:
            again = await open_incident(
                session,
                workspace_id=workspace.id,
                resource_ids=[ids["api"]],
                detected_by="watcher",
                signals=[signal(detail)],
            )
            assert again.deduped
    async with sf() as session:
        incident = await session.get(type(opened.incident), opened.incident.id)
        assert incident is not None
        assert [s["detail"][:3] for s in incident.signals] == ["8 e", "306"]
        jobs = list(await session.scalars(select(Job).where(Job.kind == REDIAGNOSE_JOB)))
        assert len(jobs) == 1 and jobs[0].payload["signals"] == [
            "log_error_spike: 306 error lines in 5 min"
        ]


def _errors_aws(n: int) -> dict[str, Any]:
    """AWS where checkout-api's logs show `n` statement timeouts in the last few minutes."""
    now = datetime.now(UTC)
    events = [
        {
            "timestamp": int((now - timedelta(seconds=20 + i)).timestamp() * 1000),
            "message": _timeout_line(i),
            "eventId": str(i),
        }
        for i in range(n)
    ]

    def logs(params: dict[str, Any]) -> dict[str, Any]:
        lo, hi = params.get("startTime", 0), params.get("endTime", 2**62)
        return {"events": [e for e in events if lo <= e["timestamp"] <= hi]}

    return {**healthy_aws("checkout-api", "checkout-worker"), "logs:FilterLogEvents": logs}


def _restart_evidence(ids: dict[str, uuid.UUID], t0: datetime) -> Any:
    ev = _evidence(
        {
            "cloudwatch_logs:/ecs/checkout-api": [
                (-2 + i * 0.1, x) for i, x in enumerate(UVICORN_INFO)
            ],
            "ecs:checkout-api": [
                (-3, "deployment ecs-svc/1 started, task definition checkout-api:18")
            ],
        },
        ids,
        [],
        t0,
    )
    return ev


def _false_alarm(ev: Any) -> str:
    line = eid(ev, "cloudwatch_logs:/ecs/checkout-api", "Started server")
    return json.dumps(
        {
            "summary": "A restart's INFO lines were counted as errors: a false alarm.",
            "root_cause": "uvicorn.error INFO lines at startup.",
            "claims": [{"text": "Startup lines.", "evidence_ids": [line]}],
            "affected_resources": ["checkout-api"],
            "confidence": "high",
            "fix_options": [],
            "false_alarm": True,
        }
    )


async def test_false_alarm_is_rejected_while_errors_and_latency_are_not_normal(
    session_factory: SessionFactory,
    workspace: Workspace,
    use_deps: Any,  # noqa: F811
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    ev = _restart_evidence(ids, _now())
    real = json.dumps(
        {
            "summary": "Searches time out after 16 s: an outage, not noise.",
            "root_cause": "Statement timeouts on /orders/search return HTTP 500.",
            "claims": [
                {
                    "text": "Restart lines.",
                    "evidence_ids": [eid(ev, "cloudwatch_logs:/ecs/checkout-api", "Started")],
                }
            ],
            "affected_resources": ["checkout-api"],
            "confidence": "medium",
            "fix_options": [],
            "false_alarm": False,
        }
    )
    llm = FakeLLM(reply(_false_alarm(ev)), reply(real), settings=settings())
    use_deps(Harness(sf, ev, llm, actions=None, aws=_errors_aws(40)))
    incident_id, run_id = await _open_api(sf, workspace.id, ids["api"])
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})
    incident, state, _ = await _state(sf, incident_id)
    assert state.root_cause is not None and not state.root_cause.false_alarm
    assert state.root_cause.summary.startswith("Searches time out")
    assert any(
        d.startswith("false alarm rejected: /ecs/checkout-api: 40 error lines")
        for d in state.dropped
    )
    retry = llm.calls[1]["messages"][-1]["content"]
    assert "not a false alarm" in retry and "40 error lines" in retry
    assert incident.status != "resolved"


async def test_a_confirmed_false_alarm_closes_without_fixes(
    session_factory: SessionFactory,
    workspace: Workspace,
    use_deps: Any,  # noqa: F811
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    ev = _restart_evidence(ids, _now())
    llm = FakeLLM(reply(_false_alarm(ev)), settings=settings())
    use_deps(Harness(sf, ev, llm, actions=None))
    incident_id, run_id = await _open_api(sf, workspace.id, ids["api"])
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})
    incident, state, _ = await _state(sf, incident_id)
    assert incident.status == "resolved" and state.fixes == []
    assert state.outcome is not None and state.outcome.reason is not None
    assert state.outcome.reason.startswith("False alarm")


async def _open_api(
    sf: SessionFactory, ws: uuid.UUID, api: uuid.UUID, detail: str = "8 error lines in 5 min"
) -> tuple[uuid.UUID, uuid.UUID]:
    async with sf() as session:
        opened = await open_incident(
            session,
            workspace_id=ws,
            resource_ids=[api],
            detected_by="watcher",
            signals=[
                Signal(
                    rule="log_error_spike",
                    resource_id=api,
                    resource_name="checkout-api",
                    detail=detail,
                    at=_now() - timedelta(minutes=2),
                )
            ],
        )
    return opened.incident.id, opened.run.id


SHA = "bff1a66c0ffee0123456789abcdef0123456789a"
MIGRATION = "api/migrations/versions/0008_recreate_customer_email_index.py"


def _index_case(ids: dict[str, uuid.UUID], t0: datetime, summary: str) -> tuple[Any, str, str]:
    ev = _evidence(
        {
            f"github:{REPO}": [(-5, f"commit {SHA[:7]} by dev: Drop index | files: m.py")],
            "cloudwatch_logs:/ecs/checkout-api": [(-1, _timeout_line(1))],
        },
        ids,
        [
            ChangeMarker(
                kind="commit",
                ref=SHA,
                at=t0 - timedelta(minutes=5),
                label="drop",
                commit=SHA,
                repo=REPO,
            )
        ],
        t0,
    )
    rc = json.dumps(
        {
            "summary": summary,
            "root_cause": "Commit bff1a66 dropped orders.customer_email; searches scan the table.",
            "claims": [
                {
                    "text": "Timeouts.",
                    "evidence_ids": [eid(ev, "cloudwatch_logs:/ecs/checkout-api", "search")],
                }
            ],
            "triggering_change": {"kind": "commit", "ref": SHA[:7], "evidence_ids": []},
            "affected_resources": ["checkout-api"],
            "confidence": "high",
            "fix_options": [{"kind": "pull_request", "description": "recreate the index"}],
        }
    )
    fix = json.dumps(
        {
            "title": "Recreate orders.customer_email index concurrently",
            "body": "Adds the index back.",
            "rationale": "Searches scan the table without it.",
            "files": [{"path": MIGRATION, "content": "CREATE INDEX CONCURRENTLY ...\n"}],
        }
    )
    return ev, rc, fix


async def test_new_signals_while_waiting_for_approval_rediagnose_before_any_fix(
    session_factory: SessionFactory,
    workspace: Workspace,
    use_deps: Any,  # noqa: F811
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    ev, rc, fix = _index_case(ids, _now(), "First look.")
    _, rc2, fix2 = _index_case(ids, _now(), "Searches time out: the index is gone.")
    llm = FakeLLM(
        reply(rc), reply(fix), reply(rc2), reply(fix2), settings=settings(dev_mode_no_ultra=True)
    )
    routes = github_routes(SHA, {"m.py": ("+op.drop_index('ix')", "x\n")})
    use_deps(Harness(sf, ev, llm, actions=FakeEmergencyActions(sf), gh=routes))
    incident_id, run_id = await _open_api(sf, workspace.id, ids["api"])
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})
    _, state, _ = await _state(sf, incident_id)
    first_approval = state.fixes[0].approval_id

    # The slow-query rule fires on the same outage while the PR waits for approval.
    async with sf() as session:
        again = await open_incident(
            session,
            workspace_id=workspace.id,
            resource_ids=[ids["api"]],
            detected_by="watcher",
            signals=[
                Signal(
                    rule="rds_cpu_latency",
                    resource_id=ids["api"],
                    resource_name="checkout-api",
                    detail="/orders/search p50 13860 ms (baseline 13 ms), CPU 56%",
                    at=_now(),
                )
            ],
        )
        job = await session.scalar(select(Job).where(Job.kind == REDIAGNOSE_JOB))
        assert again.deduped and job is not None
    await rediagnose_incident_job(sf, job.payload)

    incident, state, _ = await _state(sf, incident_id)
    assert state.rediagnosed == 1 and state.root_cause is not None
    assert state.root_cause.summary == "Searches time out: the index is gone."
    assert [f.fix.title for f in state.fixes] == [
        "Recreate orders.customer_email index concurrently"
    ]
    async with sf() as session:
        old = await session.get(Approval, first_approval)
        assert old is not None and old.status == ApprovalStatus.rejected
        assert old.decided_via == "Medic: superseded by new signals"
    assert incident.status == "waiting_approval"
    # The fix prompt says how to title it.
    assert "never mention alerts, false alarms" in fx.FIX_SYSTEM


async def test_a_decided_fix_is_not_superseded(
    session_factory: SessionFactory,
    workspace: Workspace,
    use_deps: Any,  # noqa: F811
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    ev, rc, fix = _index_case(ids, _now(), "First look.")
    llm = FakeLLM(reply(rc), reply(fix), settings=settings(dev_mode_no_ultra=True))
    routes = github_routes(SHA, {"m.py": ("+op.drop_index('ix')", "x\n")})
    use_deps(Harness(sf, ev, llm, actions=FakeEmergencyActions(sf), gh=routes))
    incident_id, run_id = await _open_api(sf, workspace.id, ids["api"])
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})
    _, state, _ = await _state(sf, incident_id)
    await _decide(sf, workspace, state.fixes[0].approval_id, Decision.approve)
    await rediagnose_incident_job(
        sf, {"incident_id": str(incident_id), "run_id": str(run_id), "signals": ["x"]}
    )
    _, after, _ = await _state(sf, incident_id)
    assert after.rediagnosed == 0 and after.fixes[0].approval_id == state.fixes[0].approval_id


# --- B4: verify waits, one chain ---------------------------------------------------------------


def test_a_rollout_in_progress_is_waiting_not_failing() -> None:
    service = {
        "deployments": [
            {"status": "PRIMARY", "rolloutState": "IN_PROGRESS"},
            {"status": "ACTIVE", "rolloutState": "COMPLETED"},
        ]
    }
    assert rollout_wait("checkout-api", service) == "checkout-api rollout IN_PROGRESS"
    merged = datetime(2026, 10, 13, 11, 22, tzinfo=UTC)
    done = {
        "deployments": [
            {
                "status": "PRIMARY",
                "rolloutState": "COMPLETED",
                "createdAt": merged - timedelta(hours=1),
            }
        ]
    }
    assert rollout_wait("checkout-api", done) is None
    assert rollout_wait("checkout-api", done, merged) == (
        "checkout-api: no deploy since the fix PR merged"
    )


async def test_verify_waits_for_the_pr_counts_checks_and_runs_one_chain(
    session_factory: SessionFactory,
    workspace: Workspace,
    use_deps: Any,  # noqa: F811
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    ev, rc, fix = _index_case(ids, _now(), "Index gone.")
    llm = FakeLLM(reply(rc), reply(fix), settings=settings(dev_mode_no_ultra=True))
    routes = github_routes(SHA, {"m.py": ("+op.drop_index('ix')", "x\n")})
    use_deps(Harness(sf, ev, llm, actions=FakeEmergencyActions(sf), gh=routes))
    incident_id, run_id = await _open_api(sf, workspace.id, ids["api"])
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})
    _, state, _ = await _state(sf, incident_id)
    await _decide(sf, workspace, state.fixes[0].approval_id, Decision.approve)
    async with sf() as session:
        from app.pipeline.resume import resume_run_job

        del session
    await resume_run_job(sf, {"run_id": str(run_id), "approval_id": "x"})
    # A second resume after the same decisions must not start a second verify chain.
    await resume_run_job(sf, {"run_id": str(run_id), "approval_id": "x"})
    async with sf() as session:
        chains = {
            j.payload.get("chain")
            for j in await session.scalars(select(Job).where(Job.kind == agent.VERIFY_JOB))
        }
    assert len(chains) == 1
    _, state, _ = await _state(sf, incident_id)
    chain = state.verify_chain
    payload = {"incident_id": str(incident_id), "run_id": str(run_id)}
    await verify_incident_job(sf, {**payload, "chain": "someone-else"})
    _, unchanged, _ = await _state(sf, incident_id)
    assert unchanged.verify_checks == 0
    await verify_incident_job(sf, {**payload, "chain": chain})
    await verify_incident_job(sf, {**payload, "chain": chain})
    incident, state, _ = await _state(sf, incident_id)
    assert state.verify is not None and state.verify.check == 2
    pr = state.fixes[0].pull_request
    assert pr is not None and state.verify.waiting == f"{pr.repo}#{pr.number} to be merged"
    assert not state.verify.ok and state.healthy_streak == 0
    assert incident.status == "verifying"
