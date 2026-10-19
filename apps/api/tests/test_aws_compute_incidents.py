"""Lambda and EC2 incidents: detection, evidence, the two emergency actions, and Medic end to
end (a failing function rolled back by its alias; an impaired instance rebooted)."""

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest

from app.actions.aws import AwsEmergencyActions, pick_alias
from app.actions.base import EmergencyActionError
from app.actions.fake import FakeEmergencyActions, FakeFunction, FakeInstance
from app.agents.medic.events import ChangeMarker, Signal
from app.agents.medic.jobs import investigate_incident_job, verify_incident_job
from app.connectors.aws import AWS_OPERATE_OPERATIONS, AWS_READ_OPERATIONS
from app.db.models import Connection, Resource, Workspace
from app.db.session import SessionFactory
from app.incidents import rules
from app.incidents.store import open_incident
from app.llm.fake import FakeLLM, reply
from app.pipeline.resume import resume_run_job
from app.policy.engine import Decision
from app.telemetry import aws_compute
from app.telemetry.collect import Evidence
from app.telemetry.evidence import line
from tests.aws_fixture import ACCOUNT, REGION, fixture_session
from tests.medic_fakes import approve_medic_action, medic_run
from tests.test_medic import Harness, _decide, _now, _state, eid, settings
from tests.test_medic import use_deps as medic_deps

pytestmark = pytest.mark.anyio

use_deps = medic_deps
FN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:orders-api"
INSTANCE = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-0abc123"


def _minutes(now: datetime, values: list[float]) -> list[tuple[datetime, float]]:
    """One value per minute, the last one a minute ago."""
    return [(now - timedelta(minutes=len(values) - i), v) for i, v in enumerate(values)]


def _metrics(now: datetime, *, errors_now: float, calls: float = 20.0) -> dict[str, Any]:
    # 25 quiet minutes, then 5 minutes with `errors_now` errors each.
    return {
        "errors": _minutes(now, [0.0] * 25 + [errors_now] * 5),
        "invocations": _minutes(now, [calls] * 30),
        "throttles": _minutes(now, [0.0] * 30),
    }


# --- detection --------------------------------------------------------------------------------


def test_a_function_whose_errors_jump_fires_and_is_down_near_every_call() -> None:
    now = _now()
    (signal,) = rules.lambda_signals(uuid.uuid4(), "orders-api", _metrics(now, errors_now=4), now)
    assert signal.rule == "lambda_errors" and not signal.down
    assert signal.detail.startswith("20 errors in 100 invocations in the last 5 minutes (20%")
    (down,) = rules.lambda_signals(uuid.uuid4(), "orders-api", _metrics(now, errors_now=19), now)
    assert down.down
    # The same error rate as before is not a spike.
    steady = _metrics(now, errors_now=4)
    steady["errors"] = _minutes(now, [4.0] * 30)
    assert rules.lambda_signals(uuid.uuid4(), "orders-api", steady, now) == []


def test_throttles_fire_on_their_own() -> None:
    now = _now()
    metrics = _metrics(now, errors_now=0)
    metrics["throttles"] = _minutes(now, [0.0] * 25 + [10.0] * 5)
    (signal,) = rules.lambda_signals(uuid.uuid4(), "orders-api", metrics, now)
    assert signal.rule == "lambda_throttles" and "concurrency limit" in signal.detail


def test_an_impaired_instance_is_down_and_a_healthy_one_is_quiet() -> None:
    now = _now()
    impaired = {
        "InstanceState": {"Name": "running"},
        "SystemStatus": {"Status": "ok"},
        "InstanceStatus": {
            "Status": "impaired",
            "Details": [
                {
                    "Name": "reachability",
                    "Status": "failed",
                    "ImpairedSince": now - timedelta(minutes=3),
                }
            ],
        },
    }
    signal = rules.ec2_signal(uuid.uuid4(), "web-1", impaired, now)
    assert signal is not None and signal.rule == "ec2_status_check" and signal.down
    assert signal.detail == "instance status impaired (reachability failed); state running"
    assert signal.at == now - timedelta(minutes=3)
    ok = {**impaired, "InstanceStatus": {"Status": "ok"}}
    assert rules.ec2_signal(uuid.uuid4(), "web-1", ok, now) is None


def test_the_alias_callers_use_is_the_one_rolled_back() -> None:
    aliases = [
        {"Name": "canary", "FunctionVersion": "7"},
        {"Name": "live", "FunctionVersion": "7"},
        {"Name": "old", "FunctionVersion": "5"},
    ]
    assert pick_alias(aliases, ["5", "6", "7"]) == {"Name": "live", "FunctionVersion": "7"}
    assert pick_alias([{"Name": "dev", "FunctionVersion": "$LATEST"}], ["5"]) is None


def test_function_evidence_marks_published_versions_and_code_updates() -> None:
    now = _now()
    config = {
        "FunctionName": "orders-api",
        "FunctionArn": FN,
        "LastModified": (now - timedelta(minutes=8)).isoformat(),
        "CodeSha256": "abcdef1234567890",
        "State": "Active",
        "Runtime": "nodejs20.x",
    }
    versions = [
        {"Version": "$LATEST"},
        {"Version": "6", "LastModified": (now - timedelta(days=3)).isoformat()},
        {
            "Version": "7",
            "LastModified": (now - timedelta(minutes=8)).isoformat(),
            "CodeSha256": "abc",
        },
    ]
    out = aws_compute.describe_function(
        config,
        versions,
        [{"Name": "live", "FunctionVersion": "7"}],
        _metrics(now, errors_now=4),
        now - timedelta(minutes=30),
        now,
    )
    assert [c.ref for c in out.changes] == [f"{FN}:7", f"{FN}:$LATEST@abcdef123456"]
    text = "\n".join(entry.message for entry in out.lines)
    assert "alias live -> version 7" in text and "4 errors, 0 throttles in 20 invocations" in text


# --- the AWS actions ----------------------------------------------------------------------------


async def _actions(
    sf: SessionFactory, ws: Workspace, responses: dict[str, Any], calls: list[str]
) -> tuple[AwsEmergencyActions, uuid.UUID]:
    async with sf() as session:
        conn = Connection(
            workspace_id=ws.id,
            kind="aws",
            name="aws",
            status="connected",
            config={"account_id": ACCOUNT, "external_id": "gc-1", "operate_role_arn": "x"},
        )
        session.add(conn)
        await session.commit()

    def operate(conn: Connection, service: str, region: str) -> Any:
        return fixture_session(responses, calls, allowed=AWS_OPERATE_OPERATIONS).client(
            service, region_name=region
        )

    def read(conn: Connection, service: str, region: str) -> Any:
        return fixture_session(responses, calls, allowed=AWS_READ_OPERATIONS).client(
            service, region_name=region
        )

    return AwsEmergencyActions(sf, client_for=operate, read_client_for=read), conn.id


async def test_lambda_alias_rollback_plans_then_moves_the_alias_once(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    alias = {"Name": "live", "FunctionVersion": "7"}
    writes: list[dict[str, Any]] = []

    def update(params: dict[str, Any]) -> dict[str, Any]:
        writes.append(params)
        alias["FunctionVersion"] = params["FunctionVersion"]
        return dict(alias)

    responses = {
        "lambda:ListAliases": {"Aliases": [alias]},
        "lambda:ListVersionsByFunction": {
            "Versions": [{"Version": "$LATEST"}, {"Version": "6"}, {"Version": "7"}]
        },
        "lambda:GetAlias": lambda p: dict(alias),
        "lambda:UpdateAlias": update,
    }
    actions, cid = await _actions(session_factory, workspace, responses, [])
    plan = await actions.plan_lambda_rollback_alias(cid, FN)
    assert (plan.current, plan.desired) == (f"{FN}:7", f"{FN}:6")
    assert plan.summary == "Point orders-api:live back at version 6 (now 7)"
    run = await medic_run(session_factory, workspace)
    token = await approve_medic_action(
        session_factory, workspace, run, action=plan.action, plan_hash=plan.plan_hash()
    )
    result = await actions.execute(plan, token, run_id=run.id)
    assert result.ok and result.details == {"alias": "live", "version": "6"}
    assert writes == [
        {
            "FunctionName": "orders-api",
            "Name": "live",
            "FunctionVersion": "6",
            "RoutingConfig": {"AdditionalVersionWeights": {}},
        }
    ]


async def test_no_alias_means_no_rollback_and_a_healthy_instance_is_not_rebooted(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    responses = {
        "lambda:ListAliases": {"Aliases": []},
        "lambda:ListVersionsByFunction": {"Versions": [{"Version": "$LATEST"}]},
        "ec2:DescribeInstanceStatus": {
            "InstanceStatuses": [
                {
                    "InstanceState": {"Name": "running"},
                    "SystemStatus": {"Status": "ok"},
                    "InstanceStatus": {"Status": "ok"},
                }
            ]
        },
    }
    actions, cid = await _actions(session_factory, workspace, responses, [])
    with pytest.raises(EmergencyActionError, match="runs as \\$LATEST"):
        await actions.plan_lambda_rollback_alias(cid, FN)
    with pytest.raises(EmergencyActionError, match="No reboot: i-0abc123 running, system ok"):
        await actions.plan_ec2_reboot_instance(cid, INSTANCE)


async def test_ec2_reboot_runs_once_and_not_after_the_instance_recovered(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    state = {"InstanceStatus": "impaired"}
    reboots: list[dict[str, Any]] = []

    def reboot(params: dict[str, Any]) -> dict[str, Any]:
        reboots.append(params)
        return {}

    responses = {
        "ec2:DescribeInstanceStatus": lambda p: {
            "InstanceStatuses": [
                {
                    "InstanceState": {"Name": "running"},
                    "SystemStatus": {"Status": "ok"},
                    "InstanceStatus": {"Status": state["InstanceStatus"]},
                }
            ]
        },
        "ec2:RebootInstances": reboot,
    }
    actions, cid = await _actions(session_factory, workspace, responses, [])
    plan = await actions.plan_ec2_reboot_instance(cid, INSTANCE)
    assert plan.current == "i-0abc123 running, system ok, instance impaired"
    run = await medic_run(session_factory, workspace)
    token = await approve_medic_action(
        session_factory, workspace, run, action=plan.action, plan_hash=plan.plan_hash()
    )
    state["InstanceStatus"] = "ok"  # it recovered while waiting for approval
    result = await actions.execute(plan, token, run_id=run.id)
    assert not result.ok and reboots == []
    state["InstanceStatus"] = "impaired"
    plan = await actions.plan_ec2_reboot_instance(cid, INSTANCE)
    token = await approve_medic_action(
        session_factory, workspace, run, action=plan.action, plan_hash=plan.plan_hash()
    )
    assert (await actions.execute(plan, token, run_id=run.id)).ok
    assert reboots == [{"InstanceIds": ["i-0abc123"]}]


# --- Medic end to end --------------------------------------------------------------------------


async def _graph(sf: SessionFactory, ws: uuid.UUID) -> dict[str, uuid.UUID]:
    async with sf() as session:
        conn = Connection(
            workspace_id=ws,
            kind="aws",
            name="aws",
            status="connected",
            config={"account_id": ACCOUNT},
        )
        session.add(conn)
        await session.flush()
        fn = Resource(
            workspace_id=ws,
            connection_id=conn.id,
            provider="aws",
            type="lambda:function",
            name="orders-api",
            external_id=FN,
            region=REGION,
            config={"log_group": "/aws/lambda/orders-api"},
        )
        vm = Resource(
            workspace_id=ws,
            connection_id=conn.id,
            provider="aws",
            type="ec2:instance",
            name="web-1",
            external_id=INSTANCE,
            region=REGION,
        )
        session.add_all([fn, vm])
        await session.commit()
        return {"fn": fn.id, "vm": vm.id}


async def _open(sf: SessionFactory, ws: uuid.UUID, signal: Signal) -> tuple[uuid.UUID, uuid.UUID]:
    async with sf() as session:
        opened = await open_incident(
            session,
            workspace_id=ws,
            resource_ids=[signal.resource_id] if signal.resource_id else [],
            detected_by="watcher",
            signals=[signal],
        )
    return opened.incident.id, opened.run.id


async def _approve_all(
    sf: SessionFactory, ws: Workspace, incident_id: uuid.UUID, run_id: uuid.UUID
) -> None:
    _, state, _ = await _state(sf, incident_id)
    for fix in state.fixes:
        await _decide(sf, ws, fix.approval_id, Decision.approve)
        await resume_run_job(sf, {"run_id": str(run_id), "approval_id": str(fix.approval_id)})


async def test_medic_rolls_a_failing_function_back_by_its_alias(
    session_factory: SessionFactory, workspace: Workspace, use_deps: Any
) -> None:
    sf = session_factory
    ids = await _graph(sf, workspace.id)
    now = _now()
    ev = Evidence()
    key = "lambda:orders-api"
    ev.sources[key] = [
        line(now - timedelta(minutes=8), "lambda", "orders-api", "version 7 published (code abc)"),
        line(
            now - timedelta(minutes=4),
            "lambda",
            "orders-api",
            "19 errors, 0 throttles in 20 invocations this minute",
        ),
    ]
    ev.truncated[key] = False
    ev.source_resources[key] = ids["fn"]
    ev.changes = [
        ChangeMarker(
            kind="deploy",
            ref=f"{FN}:7",
            at=now - timedelta(minutes=8),
            label="orders-api version 7",
            resource_id=ids["fn"],
        )
    ]
    root = {
        "summary": "Version 7 of orders-api throws on every request.",
        "root_cause": "Version 7 reads a config key that does not exist.",
        "claims": [
            {"text": "Version 7 was published.", "evidence_ids": [eid(ev, key, "published")]},
            {"text": "Nearly every call fails.", "evidence_ids": [eid(ev, key, "19 errors")]},
        ],
        "triggering_change": {"kind": "deploy", "ref": f"{FN}:7"},
        "affected_resources": ["orders-api"],
        "confidence": "high",
        "fix_options": [{"kind": "lambda_rollback_alias", "description": "Back to version 6."}],
    }
    actions = FakeEmergencyActions(
        sf, functions={FN: FakeFunction(versions=["5", "6", "7"], at="7")}
    )
    healthy: dict[str, Any] = {
        "cloudwatch:GetMetricData": {"MetricDataResults": []},
        "logs:FilterLogEvents": {"events": []},
    }
    use_deps(
        Harness(
            sf,
            ev,
            FakeLLM(reply(json.dumps(root)), settings=settings()),
            actions=actions,
            aws=healthy,
        )
    )
    signal = Signal(
        rule="lambda_errors",
        resource_id=ids["fn"],
        resource_name="orders-api",
        detail="95 errors in 100 invocations",
        at=now - timedelta(minutes=4),
        down=True,
    )
    incident_id, run_id = await _open(sf, workspace.id, signal)
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})
    incident, state, _ = await _state(sf, incident_id)
    assert incident.status == "waiting_approval", state.error
    (fix,) = [f for f in state.fixes if f.fix.kind == "emergency"]
    assert (
        fix.fix.action == "lambda_rollback_alias"
        and fix.fix.title == "Roll back orders-api to version 6"
    )
    assert [(p.before, p.after) for p in fix.fix.plan_lines] == [("7", "6")]
    await _approve_all(sf, workspace, incident_id, run_id)
    assert actions.functions[FN].at == "6"
    payload = {"incident_id": str(incident_id), "run_id": str(run_id)}
    await verify_incident_job(sf, payload)
    await verify_incident_job(sf, payload)
    incident, state, _ = await _state(sf, incident_id)
    assert incident.status == "resolved", state.verify


async def test_medic_reboots_an_impaired_instance(
    session_factory: SessionFactory, workspace: Workspace, use_deps: Any
) -> None:
    sf = session_factory
    ids = await _graph(sf, workspace.id)
    now = _now()
    ev = Evidence()
    key = "ec2:web-1"
    ev.sources[key] = [
        line(
            now - timedelta(minutes=3),
            "ec2",
            "web-1",
            "instance status impaired (reachability failed)",
        )
    ]
    ev.truncated[key] = False
    ev.source_resources[key] = ids["vm"]
    root = {
        "summary": "web-1 stopped answering: its instance status check fails.",
        "root_cause": "The operating system on web-1 is unreachable (instance check failed).",
        "claims": [
            {"text": "Reachability check failed.", "evidence_ids": [eid(ev, key, "impaired")]}
        ],
        "triggering_change": None,
        "affected_resources": ["web-1"],
        "confidence": "medium",
        "fix_options": [{"kind": "ec2_reboot_instance", "description": "Reboot web-1."}],
    }
    actions = FakeEmergencyActions(sf, instances={INSTANCE: FakeInstance()})
    healthy = {
        "ec2:DescribeInstanceStatus": {
            "InstanceStatuses": [
                {
                    "InstanceState": {"Name": "running"},
                    "SystemStatus": {"Status": "ok"},
                    "InstanceStatus": {"Status": "ok"},
                }
            ]
        },
        "cloudwatch:GetMetricData": {"MetricDataResults": []},
    }
    use_deps(
        Harness(
            sf,
            ev,
            FakeLLM(reply(json.dumps(root)), settings=settings()),
            actions=actions,
            aws=healthy,
        )
    )
    signal = Signal(
        rule="ec2_status_check",
        resource_id=ids["vm"],
        resource_name="web-1",
        detail="instance status impaired (reachability failed); state running",
        at=now - timedelta(minutes=3),
        down=True,
    )
    incident_id, run_id = await _open(sf, workspace.id, signal)
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})
    incident, state, _ = await _state(sf, incident_id)
    assert incident.status == "waiting_approval", state.error
    (fix,) = state.fixes
    assert fix.fix.action == "ec2_reboot_instance" and fix.fix.title == "Reboot web-1"
    await _approve_all(sf, workspace, incident_id, run_id)
    assert actions.instances[INSTANCE].reboots == 1
    payload = {"incident_id": str(incident_id), "run_id": str(run_id)}
    await verify_incident_job(sf, payload)
    await verify_incident_job(sf, payload)
    incident, state, _ = await _state(sf, incident_id)
    assert incident.status == "resolved", state.verify


async def test_the_watcher_checks_functions_and_instances(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    from app.connectors.aws import AwsReader
    from app.incidents.watcher import _targets, check

    ids = await _graph(session_factory, workspace.id)
    now = _now()
    metrics = _metrics(now, errors_now=19)
    responses = {
        "cloudwatch:GetMetricData": {
            "MetricDataResults": [
                {"Id": k, "Timestamps": [t for t, _ in v], "Values": [x for _, x in v]}
                for k, v in metrics.items()
            ]
        },
        "logs:FilterLogEvents": {"events": []},
        "ec2:DescribeInstanceStatus": {
            "InstanceStatuses": [
                {
                    "InstanceState": {"Name": "running"},
                    "SystemStatus": {"Status": "impaired", "Details": []},
                    "InstanceStatus": {"Status": "ok"},
                }
            ]
        },
    }
    reader = AwsReader(fixture_session(responses, []))
    targets = {t.resource.id: t for t in await _targets(session_factory)}
    assert [s.rule for s in check(targets[ids["fn"]], reader, now)] == ["lambda_errors"]
    assert [s.rule for s in check(targets[ids["vm"]], reader, now)] == ["ec2_status_check"]
