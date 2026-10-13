"""Tuning from the first live runs on checkout-demo (Phase 5 M5): spike floors, /health noise,
request logs without a path, secret version ids, the newest-first log read, the slow-query rule
on the missing-index numbers, and deploys stamped at their start."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from app.agents.medic.schemas import EvidenceItem
from app.connectors.aws import AwsReader
from app.db.models import Workspace
from app.db.session import SessionFactory
from app.incidents import rules
from app.incidents.watcher import _targets
from app.telemetry import cloudwatch_logs, ecs, rds, secrets
from app.telemetry.collect import Evidence
from tests.test_incidents_detection import _demo

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
DB = uuid.uuid4()


def ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


def request(path: str, ms: float) -> str:
    return json.dumps({"msg": "request", "method": "GET", "path": path, "duration_ms": ms})


def traffic(
    minutes: range, p50: float, *, path: str = "/orders/search", per_minute: int = 6
) -> list[tuple[datetime, str]]:
    """`per_minute` requests in each minute `ago(m)`, all at `p50` ms."""
    return [
        (ago(m) + timedelta(seconds=5 * i), request(path, p50))
        for m in minutes
        for i in range(per_minute)
    ]


class FakeLogs:
    """FilterLogEvents as AWS answers it: the time range applied, oldest first."""

    def __init__(self, events: list[tuple[datetime, str]]) -> None:
        self.events = sorted(events)
        self.calls = 0

    def paginate(
        self, service: str, region: str | None, operation: str, key: str, *, limit: int, **p: Any
    ) -> list[dict[str, Any]]:
        assert (service, operation) == ("logs", "FilterLogEvents")
        self.calls += 1
        start, end = p["startTime"], p["endTime"]
        return [
            {"timestamp": int(at.timestamp() * 1000), "message": m}
            for at, m in self.events
            if start <= at.timestamp() * 1000 <= end
        ][:limit]


# --- 1. RDS spikes need an absolute floor -----------------------------------------------------


def test_tiny_read_latency_against_an_idle_baseline_is_not_a_spike() -> None:
    idle = [(ago(30 - i), 0.0) for i in range(20)] + [(ago(10 - i), 2.4) for i in range(10)]
    lines = rds.metric_lines("db", "read_latency", idle, "ms", 20.0)
    assert not any("[SPIKE]" in x.message for x in lines)
    real = [(ago(30 - i), 5.0) for i in range(20)] + [(ago(10 - i), 45.0) for i in range(10)]
    assert any(
        "[SPIKE]" in x.message for x in rds.metric_lines("db", "read_latency", real, "ms", 20.0)
    )
    floors = {m[0]: m[4] for m in rds.METRICS}
    assert floors["read_latency"] == 20.0 and floors["cpu"] == 30.0


def test_cpu_under_the_floor_is_not_a_spike_even_at_4x() -> None:
    points = [(ago(30 - i), 4.5) for i in range(20)] + [(ago(10 - i), 20.0) for i in range(10)]
    assert not any("[SPIKE]" in x.message for x in rds.metric_lines("db", "cpu", points, "%", 30.0))


# --- 2/3. latency summaries: /health noise, lines without a path -------------------------------


def test_health_jumps_are_summarized_but_never_slow() -> None:
    events = traffic(range(20, 4, -1), 3, path="/health") + traffic(
        range(4, 0, -1), 85, path="/health"
    )
    out = cloudwatch_logs.latency_summaries(events, "/ecs/checkout-api")
    assert out and all("path=/health" in x.message for x in out)
    assert not any("[SLOW]" in x.message for x in out)


def test_a_slow_path_minute_is_marked() -> None:
    events = traffic(range(20, 4, -1), 9) + traffic(range(4, 0, -1), 48)
    slow = [x for x in cloudwatch_logs.latency_summaries(events, "g") if "[SLOW]" in x.message]
    assert len(slow) == 4
    assert "path=/orders/search requests=6 p50=48ms" in slow[0].message
    assert "(baseline p50 9ms) [SLOW]" in slow[0].message


def test_request_logs_without_a_path_get_no_summary() -> None:
    no_path = [(ago(1), json.dumps({"msg": "job done", "duration_ms": 12}))] * 6
    no_path += [(ago(1), json.dumps({"path": "", "duration_ms": 12}))]
    no_path += [(ago(1), "worker tick latency_ms=4 path=")]
    assert cloudwatch_logs.latency_summaries(no_path, "g") == []
    assert cloudwatch_logs.request_latency(request("/orders/search?q=x", 9)) == (
        "/orders/search",
        9,
    )
    full_url = json.dumps({"url": "http://api:8000/orders?q=1", "latency_ms": 3})
    assert cloudwatch_logs.request_latency(full_url) == ("/orders", 3)


def test_medic_weights_slow_minutes_but_not_health() -> None:
    from app.agents.medic.evidence import score

    def item(text: str) -> EvidenceItem:
        return EvidenceItem(id="ev-1", source="s", timestamp="t", resource_id=None, line=text)

    health = "latency summary path=/health requests=6 p50=150ms p95=900ms max=900ms"
    slow_path = "latency summary path=/orders/search requests=6 p50=150ms p95=190ms max=600ms"
    marked = "latency summary path=/orders/search requests=6 p50=48ms p95=190ms max=600ms [SLOW]"
    assert score(item(health), set()) == 0
    assert score(item(slow_path), set()) > 0
    assert score(item(marked), set()) >= 3


# --- 4. secret versions -----------------------------------------------------------------------


def test_secret_versions_show_the_whole_version_id() -> None:
    secret = {
        "Name": "checkout/database-url",
        "LastChangedDate": ago(10),
        "VersionIdsToStages": {
            "terraform-20261013093000000000000001": ["AWSPREVIOUS"],
            "terraform-20261013095000000000000002": ["AWSCURRENT"],
        },
    }
    text = " ".join(x.message for x in secrets.describe(secret, ago(30), NOW).lines)
    assert "AWSCURRENT=terraform-20261013095000000000000002" in text
    assert "AWSCURRENT=terrafor," not in text


# --- notes stay out of the evidence -----------------------------------------------------------


def test_resources_without_a_reader_are_skipped_not_notes() -> None:
    evidence = Evidence(skipped=["no reader for ec2:security-group sg-1"])
    assert evidence.notes == [] and evidence.texts() == {}


# --- B1. busy log groups: the newest lines are kept -------------------------------------------


def test_a_busy_log_group_keeps_the_newest_events() -> None:
    # 40 minutes at 500 events a minute: 20k events, 5k read.
    events = [(ago(40) + timedelta(seconds=0.12 * i), f"line {i}") for i in range(20_000)]
    fake = FakeLogs(events)
    reader = cast(AwsReader, fake)
    got, complete_from = cloudwatch_logs.read_newest(reader, None, "g", ago(40), NOW, 5_000)
    assert len(got) <= 5_000 and got[-1][1] == "line 19999"
    assert got == sorted(got) and complete_from is not None
    assert complete_from <= ago(9)  # about the last 10 minutes, not the first
    assert all(at >= complete_from for at, _ in got)
    assert fake.calls < 30


def test_a_quiet_log_group_is_read_whole_and_says_nothing() -> None:
    events = [(ago(m), f"line {m}") for m in range(39, 0, -1)]
    got, complete_from = cloudwatch_logs.read_newest(
        cast(AwsReader, FakeLogs(events)), None, "g", ago(40), NOW, 5_000
    )
    assert [m for _, m in got] == [m for _, m in events] and complete_from is None


def test_the_reader_says_what_it_did_not_read() -> None:
    events = [(ago(40) + timedelta(seconds=0.12 * i), f"line {i}") for i in range(20_000)]
    out = cloudwatch_logs.read_log_group(cast(AwsReader, FakeLogs(events)), None, "g", ago(40), NOW)
    assert out.truncated
    assert any(x.message.startswith("read the newest") for x in out.lines)


def test_log_error_rate_uses_only_the_baseline_that_was_read() -> None:
    # Busy group: only the last 8 minutes were read; 1 error/min throughout is not a spike.
    events = [(ago(8) + timedelta(seconds=0.096 * i), "GET /x 200") for i in range(5_000)]
    events += [(ago(m), "ERROR boom") for m in range(8, 0, -1)]
    events = sorted(events)[-rules.MAX_LOG_EVENTS :]
    assert rules.log_signal(DB, "svc", events, NOW) is None


# --- B2. slow queries: p50 and database CPU, live missing-index numbers -----------------------


def _cpu(normal: float, broken: float, broken_minutes: int) -> list[tuple[datetime, float]]:
    return [(ago(m), broken if m <= broken_minutes else normal) for m in range(30, 0, -1)]


def test_missing_index_numbers_open_a_slow_query_incident() -> None:
    # Normal: p50 8-10 ms, CPU 4-5 %. Broken: p50 44-53 ms, CPU 16-25 %, for the last 3 minutes.
    events = traffic(range(30, 3, -1), 9) + traffic(range(3, 0, -1), 48)
    events += traffic(range(30, 0, -1), 3, path="/health")
    signal = rules.slow_query_signal(DB, "checkout-orders-db", _cpu(4.5, 20, 3), events, NOW)
    assert signal is not None and signal.rule == "rds_cpu_latency"
    assert signal.at == ago(3)
    assert "/orders/search p50 48 ms (baseline 9 ms)" in signal.detail
    assert "CPU 20%" in signal.detail and "3 minutes in a row" in signal.detail


def test_one_slow_minute_or_low_cpu_or_health_noise_is_not_an_incident() -> None:
    base = traffic(range(30, 5, -1), 9)
    one = base + traffic(range(1, 0, -1), 48)
    assert rules.slow_query_signal(DB, "db", _cpu(4.5, 20, 1), one, NOW) is None
    slow = base + traffic(range(5, 0, -1), 48)
    assert rules.slow_query_signal(DB, "db", _cpu(4.5, 10, 5), slow, NOW) is None
    health = base + traffic(range(30, 5, -1), 3, path="/health")
    health += traffic(range(5, 0, -1), 9) + traffic(range(5, 0, -1), 90, path="/health")
    assert rules.slow_query_signal(DB, "db", _cpu(4.5, 20, 5), health, NOW) is None


def test_p95_outliers_alone_do_not_fire() -> None:
    # Normal traffic with a 600 ms outlier every minute: p95 jumps, p50 stays.
    events = traffic(range(30, 0, -1), 9)
    events += [
        (ago(m) + timedelta(seconds=59), request("/orders/search", 600)) for m in range(5, 0, -1)
    ]
    assert rules.slow_query_signal(DB, "db", _cpu(4.5, 20, 5), events, NOW) is None


def test_without_request_logs_the_old_cpu_and_read_latency_rule_needs_20ms() -> None:
    cpu = [(ago(m), 85.0 if m <= 5 else 5.0) for m in range(30, 0, -1)]
    lat = [(ago(m), 2.4 if m <= 5 else 0.1) for m in range(30, 0, -1)]
    assert rules.rds_signal(DB, "db", cpu, lat, NOW) is None
    lat = [(ago(m), 25.0 if m <= 5 else 1.0) for m in range(30, 0, -1)]
    assert rules.rds_signal(DB, "db", cpu, lat, NOW) is not None


# --- B3. a deploy is stamped when it starts ---------------------------------------------------


def test_a_deploy_has_a_line_when_it_started_not_only_when_it_finished() -> None:
    service = {
        "serviceName": "checkout-api",
        "runningCount": 1,
        "desiredCount": 1,
        "deployments": [
            {
                "id": "ecs-svc/7",
                "status": "PRIMARY",
                "taskDefinition": "arn:aws:ecs:us-east-2:1:task-definition/checkout-api:43",
                "rolloutState": "COMPLETED",
                "createdAt": ago(12),
                "updatedAt": ago(4),
            }
        ],
    }
    out = ecs.describe(service, [], ago(30), NOW)
    started = [x for x in out.lines if "started" in x.message]
    assert [x.at for x in started] == [ago(12)]
    assert "deployment ecs-svc/7 started: task definition checkout-api:43" in started[0].message
    assert [c.at for c in out.changes] == [ago(12)]


@pytest.mark.anyio
async def test_the_watcher_reads_a_database_with_its_readers_log_groups(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    ids = await _demo(session_factory, workspace.id)
    targets = {t.resource.id: t for t in await _targets(session_factory)}
    # checkout-api and checkout-worker read the database; only the worker has a log group.
    assert targets[ids["db"]].log_groups == ["/ecs/checkout-worker"]
    assert targets[ids["worker"]].log_groups == ["/ecs/checkout-worker"]
