"""Load balancers, Aurora clusters, and slow requests in common access-log formats."""

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest

from app.actions.fake import FakeEmergencyActions, FakeService
from app.agents.medic.events import ChangeMarker, Signal
from app.agents.medic.jobs import investigate_incident_job
from app.connectors.aws import AwsReader
from app.db.models import Connection, Edge, Resource, Workspace
from app.db.session import SessionFactory
from app.discovery.aws import lb_dimension, normalize_load_balancer
from app.discovery.types import DiscoveredResource
from app.graph.edges import infer_edges, key
from app.incidents import rules
from app.incidents.store import open_incident
from app.llm.fake import FakeLLM, reply
from app.telemetry import aws_compute
from app.telemetry.cloudwatch_logs import request_latency
from app.telemetry.collect import Evidence
from app.telemetry.evidence import line
from tests.aws_fixture import ACCOUNT, REGION, fixture_session
from tests.test_medic import TD, Harness, _now, _state, eid, settings
from tests.test_medic import use_deps as medic_deps

pytestmark = pytest.mark.anyio

use_deps = medic_deps
LB_ARN = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/shop/abc123"
TG_ARN = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/shop-api/def456"
TG = "targetgroup/shop-api/def456"
LB = "app/shop/abc123"


def _minutes(now: datetime, values: list[float]) -> list[tuple[datetime, float]]:
    return [(now - timedelta(minutes=len(values) - i), v) for i, v in enumerate(values)]


def _alb(
    now: datetime,
    *,
    five_xx: float = 0.0,
    unhealthy: float = 0.0,
    healthy: float = 2.0,
    p95: float = 0.1,
) -> dict[str, Any]:
    return {
        "requests": _minutes(now, [100.0] * 30),
        "target5xx": _minutes(now, [0.0] * 25 + [five_xx] * 5),
        "elb5xx": _minutes(now, [0.0] * 30),
        "p95": _minutes(now, [0.1] * 25 + [p95] * 5),
        "unhealthy0": _minutes(now, [0.0] * 25 + [unhealthy] * 5),
        "healthy0": _minutes(now, [2.0] * 25 + [healthy] * 5),
    }


# --- detection --------------------------------------------------------------------------------


def test_a_5xx_spike_and_targets_that_all_fail_are_down() -> None:
    now = _now()
    (spike,) = rules.alb_signals(uuid.uuid4(), "shop", _alb(now, five_xx=20), [TG], now)
    assert spike.rule == "alb_5xx" and not spike.down
    assert spike.detail.startswith("100 5xx responses in 500 requests")
    (down, *_) = rules.alb_signals(uuid.uuid4(), "shop", _alb(now, five_xx=95), [TG], now)
    assert down.down
    signals = {
        s.rule: s
        for s in rules.alb_signals(
            uuid.uuid4(), "shop", _alb(now, unhealthy=2, healthy=0), [TG], now
        )
    }
    assert signals["alb_unhealthy_targets"].down
    assert (
        signals["alb_unhealthy_targets"].detail
        == "unhealthy targets: shop-api: 2 unhealthy, 0 healthy"
    )
    assert rules.alb_signals(uuid.uuid4(), "shop", _alb(now), [TG], now) == []


def test_slow_responses_need_two_minutes_over_a_second_and_three_times_the_baseline() -> None:
    now = _now()
    (slow,) = rules.alb_signals(uuid.uuid4(), "shop", _alb(now, p95=2.5), [TG], now)
    assert (
        slow.rule == "alb_latency" and "p95 response time 2.50 s (baseline 0.10 s)" in slow.detail
    )
    assert rules.alb_signals(uuid.uuid4(), "shop", _alb(now, p95=0.5), [TG], now) == []


def test_aurora_clusters_use_the_cluster_dimension() -> None:
    seen: list[dict[str, Any]] = []

    def metrics(params: dict[str, Any]) -> dict[str, Any]:
        seen.append(params)
        return {"MetricDataResults": []}

    reader = AwsReader(fixture_session({"cloudwatch:GetMetricData": metrics}, []))
    assert (
        rules.rds_check(reader, REGION, uuid.uuid4(), "orders", [], _now(), "DBClusterIdentifier")
        is None
    )
    dims = seen[0]["MetricDataQueries"][0]["MetricStat"]["Metric"]["Dimensions"]
    assert dims == [{"Name": "DBClusterIdentifier", "Value": "orders"}]


# --- discovery and the graph --------------------------------------------------------------------


def _resource(type_: str, name: str, external_id: str, **config: Any) -> DiscoveredResource:
    return DiscoveredResource(
        provider="aws",
        type=type_,
        external_id=external_id,
        name=name,
        account=ACCOUNT,
        region=REGION,
        config=config,
    )


def test_a_balancer_calls_what_its_target_groups_route_to() -> None:
    lb = normalize_load_balancer(
        {
            "LoadBalancerArn": LB_ARN,
            "LoadBalancerName": "shop",
            "DNSName": "shop.elb",
            "State": {"Code": "active"},
        },
        [
            {
                "TargetGroupArn": TG_ARN,
                "TargetGroupName": "shop-api",
                "TargetType": "ip",
                "targets": ["i-0abc"],
            }
        ],
        REGION,
        ACCOUNT,
    )
    assert lb.config["dimension"] == LB and lb.config["target_groups"][0]["dimension"] == TG
    assert lb_dimension(LB_ARN) == LB
    api = _resource(
        "ecs:service",
        "shop-api",
        f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/shop/shop-api",
        load_balancers=[{"target_group": TG_ARN}],
        network={"security_groups": []},
        containers=[],
    )
    vm = _resource("ec2:instance", "web-1", f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-0abc")
    other = _resource(
        "ecs:service",
        "worker",
        f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/shop/worker",
        load_balancers=[],
        network={"security_groups": []},
        containers=[],
    )
    found = {
        (e.source, e.target)
        for e in infer_edges([lb, api, vm, other], []).edges
        if e.rule == "target_group"
    }
    assert found == {(key(lb), key(api)), (key(lb), key(vm))}


# --- access logs ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            json.dumps({"level": 30, "req": {"url": "/orders?id=1"}, "responseTime": 12.5}),
            ("/orders", 12.5),
        ),
        (
            json.dumps({"method": "GET", "path": "/orders", "status": 200, "duration": 88.2}),
            ("/orders", 88.2),
        ),
        (
            json.dumps({"request": "GET /orders HTTP/1.1", "request_time": "0.153"}),
            ("/orders", 153.0),
        ),
        ("GET /orders 200 12.345 ms - 512", ("/orders", 12.345)),
        (
            '[GIN] 2026/10/19 - 10:00:00 | 200 |   12.5ms |  1.2.3.4 | GET      "/orders"',
            ("/orders", 12.5),
        ),
        (
            '1.2.3.4 - - [19/Oct/2026:10:00:00 +0000] "GET /orders?x=1 HTTP/1.1" 200 512 "-" '
            '"curl" 0.153',
            ("/orders", 153.0),
        ),
        (
            '1.2.3.4 - - [x] "POST /pay HTTP/1.1" 502 0 "-" "x" rt=2.500 urt="2.499"',
            ("/pay", 2500.0),
        ),
        (json.dumps({"duration": 5}), None),
        ("plain text 0.153", None),
    ],
)
def test_request_latency_reads_common_access_log_formats(
    message: str, expected: tuple[str, float] | None
) -> None:
    found = request_latency(message)
    assert (found and (found[0], round(found[1], 3))) == expected or found == expected


def test_balancer_evidence_says_why_targets_fail() -> None:
    now = _now()
    config = {
        "state": "active",
        "dns_name": "shop.elb",
        "target_groups": [
            {"arn": TG_ARN, "name": "shop-api", "dimension": TG, "health_check_path": "/health"}
        ],
    }
    health = {
        TG_ARN: [
            {
                "Target": {"Id": "10.0.0.5"},
                "TargetHealth": {
                    "State": "unhealthy",
                    "Reason": "Target.ResponseCodeMismatch",
                    "Description": "Health checks failed with these codes: [500]",
                },
            },
            {"Target": {"Id": "10.0.0.6"}, "TargetHealth": {"State": "healthy"}},
        ]
    }
    out = aws_compute.describe_balancer(
        "shop", config, _alb(now, five_xx=20), health, now - timedelta(minutes=30), now
    )
    text = "\n".join(entry.message for entry in out.lines)
    assert "20 target 5xx, 0 balancer 5xx in 100 requests" in text
    assert (
        "target group shop-api: 1/2 healthy (health check /health); "
        "10.0.0.5 unhealthy Target.ResponseCodeMismatch" in text
    )


# --- Medic: a failing balancer rolls back the service behind it ---------------------------------


async def test_medic_rolls_back_the_just_deployed_service_behind_a_failing_balancer(
    session_factory: SessionFactory, workspace: Workspace, use_deps: Any
) -> None:
    sf = session_factory
    now = _now()
    async with sf() as session:
        conn = Connection(
            workspace_id=workspace.id,
            kind="aws",
            name="aws",
            status="connected",
            config={"account_id": ACCOUNT},
        )
        session.add(conn)
        await session.flush()
        lb = Resource(
            workspace_id=workspace.id,
            connection_id=conn.id,
            provider="aws",
            type="elasticloadbalancing:loadbalancer/app",
            name="shop",
            external_id=LB_ARN,
            region=REGION,
            config={
                "dimension": LB,
                "target_groups": [{"arn": TG_ARN, "name": "shop-api", "dimension": TG}],
            },
        )
        api = Resource(
            workspace_id=workspace.id,
            connection_id=conn.id,
            provider="aws",
            type="ecs:service",
            name="shop-api",
            external_id=f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/shop/shop-api",
            region=REGION,
            config={"cluster_arn": "shop"},
        )
        session.add_all([lb, api])
        await session.flush()
        session.add(Edge(workspace_id=workspace.id, from_id=lb.id, to_id=api.id, kind="calls"))
        await session.commit()
        lb_id, api_id = lb.id, api.id
    ev = Evidence()
    key_ = "alb:shop"
    ev.sources[key_] = [
        line(
            now - timedelta(minutes=3),
            "alb",
            "shop",
            "95 target 5xx, 0 balancer 5xx in 100 requests",
        )
    ]
    ev.truncated[key_] = False
    ev.source_resources[key_] = lb_id
    ev.changes = [
        ChangeMarker(
            kind="deploy",
            ref=f"{TD}/shop-api:9",
            at=now - timedelta(minutes=6),
            label="shop-api deployment of shop-api:9",
            resource_id=api_id,
        )
    ]
    root = {
        "summary": "shop-api:9 returns 500 on every request.",
        "root_cause": "The deploy of shop-api:9 broke the API behind the balancer.",
        "claims": [
            {"text": "Nearly every request fails.", "evidence_ids": [eid(ev, key_, "target 5xx")]}
        ],
        "triggering_change": {"kind": "deploy", "ref": f"{TD}/shop-api:9"},
        "affected_resources": ["shop"],
        "confidence": "high",
        "fix_options": [{"kind": "ecs_rollback_service", "description": "Back to shop-api:8."}],
    }
    actions = FakeEmergencyActions(
        sf,
        services={"shop/shop-api": FakeService(revisions=[f"{TD}/shop-api:8", f"{TD}/shop-api:9"])},
    )
    use_deps(
        Harness(sf, ev, FakeLLM(reply(json.dumps(root)), settings=settings()), actions=actions)
    )
    async with sf() as session:
        opened = await open_incident(
            session,
            workspace_id=workspace.id,
            resource_ids=[lb_id],
            detected_by="watcher",
            signals=[
                Signal(
                    rule="alb_5xx",
                    resource_id=lb_id,
                    resource_name="shop",
                    detail="95% 5xx",
                    at=now - timedelta(minutes=3),
                    down=True,
                )
            ],
        )
    await investigate_incident_job(
        sf, {"incident_id": str(opened.incident.id), "run_id": str(opened.run.id)}
    )
    incident, state, _ = await _state(sf, opened.incident.id)
    assert incident.status == "waiting_approval", state.error
    (fix,) = [f for f in state.fixes if f.fix.kind == "emergency"]
    assert fix.fix.action == "ecs_rollback_service" and fix.fix.plan is not None
    assert fix.fix.plan.params["service"] == "shop-api"
