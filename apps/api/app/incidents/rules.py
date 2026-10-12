"""Detector rules: when a linked resource is broken enough to open an incident.

Each rule is a pure function over AWS API responses (so it is tested on fixtures), plus a fetch
helper that makes the read-only calls through `AwsReader`. Rules look at the last few minutes;
the watcher runs them every INCIDENT_WATCH_INTERVAL_SECONDS and dedupes into open incidents.

| Rule | Fires when |
|---|---|
| health_check | tasks stopped for failing container health checks, or health-check failure events |
| ecs_deployment_failed | the primary deployment's rollout state is FAILED |
| ecs_tasks_stopped | tasks stopped with a non-zero exit code (137 / OutOfMemoryError included) |
| log_error_spike | error lines in the last 5 minutes at 3x the rate of the 25 before, at least 5 |
| rds_cpu_latency | CPU at 2x baseline and >= 40 %, and read latency at 2x baseline and >= 2 ms |

Thresholds are constants here; tune them against the live demo (M5).
"""

import re
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

from app.agents.medic.events import Signal
from app.connectors.aws import AwsReader

RECENT = timedelta(minutes=5)
BASELINE = timedelta(minutes=25)
MIN_ERRORS = 5
ERROR_RATE_FACTOR = 3.0
CPU_FLOOR = 40.0
LATENCY_FLOOR_MS = 2.0
SPIKE_FACTOR = 2.0
MAX_STOPPED_TASKS = 50
MAX_LOG_EVENTS = 5_000

HEALTH = re.compile(r"(?i)health ?checks?|unhealthy")
_ERROR = re.compile(
    r"(?i)\b(error|exception|traceback|fatal|critical|failed|unreachable|refused|"
    r"timed? ?out|killed|outofmemory|panic)\b|\"level\": ?\"(error|critical)\"|\s5\d\d\s"
)


def to_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value / 1000, UTC)
    return None


def _recent(at: datetime | None, now: datetime) -> bool:
    return at is not None and now - RECENT <= at <= now


# --- ECS --------------------------------------------------------------------------------------


def ecs_signals(
    resource_id: Any, service: dict[str, Any], tasks: list[dict[str, Any]], now: datetime
) -> list[Signal]:
    name = str(service.get("serviceName") or "service")
    out: list[Signal] = []
    running, desired = service.get("runningCount", 0), service.get("desiredCount", 0)
    state = f"{running}/{desired} tasks running"

    for d in service.get("deployments", []):
        if d.get("status") == "PRIMARY" and d.get("rolloutState") == "FAILED":
            at = to_time(d.get("updatedAt")) or now
            reason = d.get("rolloutStateReason") or "rollout failed"
            out.append(
                Signal(
                    rule="ecs_deployment_failed",
                    resource_id=resource_id,
                    resource_name=name,
                    detail=f"deployment failed: {reason} ({state})"[:500],
                    at=at,
                )
            )

    stopped = [t for t in tasks if _recent(to_time(t.get("stoppedAt")), now)]
    health = [t for t in stopped if HEALTH.search(str(t.get("stoppedReason", "")))]
    health_events = [
        e
        for e in service.get("events", [])
        if _recent(to_time(e.get("createdAt")), now)
        and HEALTH.search(str(e.get("message", "")))
        and "fail" in str(e.get("message", "")).lower()
    ]
    if health or health_events:
        times = [to_time(t.get("stoppedAt")) for t in health] + [
            to_time(e.get("createdAt")) for e in health_events
        ]
        at = min(t for t in times if t is not None)
        out.append(
            Signal(
                rule="health_check",
                resource_id=resource_id,
                resource_name=name,
                detail=f"{len(health)} tasks stopped failing health checks ({state})",
                at=at,
            )
        )

    crashed = []
    for t in stopped:
        if t in health:
            continue
        codes = [c.get("exitCode") for c in t.get("containers", [])]
        if any(code not in (None, 0) for code in codes):
            crashed.append((t, codes))
    if crashed:
        oom = sum(
            1
            for t, codes in crashed
            if 137 in codes or "OutOfMemory" in str(t.get("stoppedReason", ""))
        )
        codes_seen = sorted({c for _, codes in crashed for c in codes if c not in (None, 0)})
        detail = f"{len(crashed)} tasks stopped with exit code {', '.join(map(str, codes_seen))}"
        if oom:
            detail += f" ({oom} OutOfMemoryError)"
        at = min(to_time(t.get("stoppedAt")) or now for t, _ in crashed)
        out.append(
            Signal(
                rule="ecs_tasks_stopped",
                resource_id=resource_id,
                resource_name=name,
                detail=f"{detail}; {state}",
                at=at,
            )
        )
    return out


def fetch_ecs(
    reader: AwsReader, region: str | None, cluster: str, service: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Blocking (boto3)."""
    found = reader.call("ecs", region, "DescribeServices", cluster=cluster, services=[service])
    services = found.get("services", [])
    if not services:
        return None, []
    arns = reader.paginate(
        "ecs",
        region,
        "ListTasks",
        "taskArns",
        limit=MAX_STOPPED_TASKS,
        cluster=cluster,
        serviceName=service,
        desiredStatus="STOPPED",
    )
    tasks: list[dict[str, Any]] = []
    if arns:
        tasks = reader.call("ecs", region, "DescribeTasks", cluster=cluster, tasks=arns).get(
            "tasks", []
        )
    return services[0], tasks


# --- logs -------------------------------------------------------------------------------------


def log_signal(
    resource_id: Any, name: str, events: list[tuple[datetime, str]], now: datetime
) -> Signal | None:
    errors = [at for at, message in events if _ERROR.search(message)]
    recent = [at for at in errors if now - RECENT <= at <= now]
    before = [at for at in errors if now - RECENT - BASELINE <= at < now - RECENT]
    recent_rate = len(recent) / RECENT.total_seconds() * 60
    base_rate = len(before) / BASELINE.total_seconds() * 60
    if len(recent) < MIN_ERRORS or recent_rate < ERROR_RATE_FACTOR * base_rate + 0.5:
        return None
    return Signal(
        rule="log_error_spike",
        resource_id=resource_id,
        resource_name=name,
        detail=(
            f"{len(recent)} error lines in the last 5 minutes "
            f"({recent_rate:.1f}/min, before {base_rate:.1f}/min)"
        ),
        at=min(recent),
    )


def fetch_logs(
    reader: AwsReader, region: str | None, log_group: str, now: datetime
) -> list[tuple[datetime, str]]:
    """Blocking (boto3)."""
    start = now - RECENT - BASELINE
    raw = reader.paginate(
        "logs",
        region,
        "FilterLogEvents",
        "events",
        limit=MAX_LOG_EVENTS,
        logGroupName=log_group,
        startTime=int(start.timestamp() * 1000),
        endTime=int(now.timestamp() * 1000),
    )
    return [
        (at, str(e.get("message", "")))
        for e in raw
        if (at := to_time(e.get("timestamp"))) is not None
    ]


# --- RDS --------------------------------------------------------------------------------------


def rds_signal(
    resource_id: Any,
    name: str,
    cpu: list[tuple[datetime, float]],
    latency_ms: list[tuple[datetime, float]],
    now: datetime,
) -> Signal | None:
    def split(points: list[tuple[datetime, float]]) -> tuple[float, float, datetime | None]:
        recent = [(t, v) for t, v in points if t >= now - RECENT]
        before = [v for t, v in points if t < now - RECENT]
        if not recent or not before:
            return 0.0, 0.0, None
        return statistics.mean(v for _, v in recent), statistics.median(before), recent[0][0]

    cpu_now, cpu_base, cpu_at = split(sorted(cpu))
    lat_now, lat_base, lat_at = split(sorted(latency_ms))
    cpu_spike = cpu_now >= max(CPU_FLOOR, SPIKE_FACTOR * cpu_base)
    lat_spike = lat_now >= max(LATENCY_FLOOR_MS, SPIKE_FACTOR * lat_base)
    if not (cpu_spike and lat_spike and cpu_at and lat_at):
        return None
    return Signal(
        rule="rds_cpu_latency",
        resource_id=resource_id,
        resource_name=name,
        detail=(
            f"CPU {cpu_now:.0f}% (baseline {cpu_base:.0f}%), read latency {lat_now:.1f} ms "
            f"(baseline {lat_base:.1f} ms)"
        ),
        at=min(cpu_at, lat_at),
    )


def fetch_rds(
    reader: AwsReader, region: str | None, db: str, now: datetime
) -> tuple[list[tuple[datetime, float]], list[tuple[datetime, float]]]:
    """Blocking (boto3). CPU in percent, read latency in milliseconds."""

    def query(metric_id: str, name: str) -> dict[str, Any]:
        return {
            "Id": metric_id,
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/RDS",
                    "MetricName": name,
                    "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": db}],
                },
                "Period": 60,
                "Stat": "Average",
            },
        }

    results = reader.call(
        "cloudwatch",
        region,
        "GetMetricData",
        MetricDataQueries=[query("cpu", "CPUUtilization"), query("lat", "ReadLatency")],
        StartTime=now - RECENT - BASELINE,
        EndTime=now,
    ).get("MetricDataResults", [])
    by_id = {r.get("Id"): r for r in results}

    def points(metric_id: str, scale: float) -> list[tuple[datetime, float]]:
        r = by_id.get(metric_id) or {}
        return [
            (t, float(v) * scale)
            for t, v in zip(r.get("Timestamps", []), r.get("Values", []), strict=False)
            if isinstance(t, datetime)
        ]

    return points("cpu", 1.0), points("lat", 1000.0)
