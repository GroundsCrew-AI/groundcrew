"""Lambda and EC2 readers, read live through AwsReader (read-only).

Lambda: the function's configuration (state, last update), its published versions (each one
published in the window is a `deploy` change marker, ref "<function ARN>:<version>"; a code
update of $LATEST in the window is one too, ref "<function ARN>:$LATEST@<CodeSha256 prefix>"),
its aliases and the version each one points at, per-minute errors / invocations / throttles,
and the error lines of its log group.

EC2: the instance's state and why it last changed, its status checks and scheduled events,
and per-minute CPU and failed status checks.
"""

from datetime import datetime, timedelta
from typing import Any

from app.agents.medic.events import ChangeMarker
from app.connectors.aws import AwsReader
from app.incidents import rules
from app.telemetry import cloudwatch_logs
from app.telemetry.evidence import ReaderOutput, line, parse_time

LAMBDA = "lambda"
EC2 = "ec2"
MAX_VERSIONS = 50


def _in(at: datetime | None, since: datetime, until: datetime) -> bool:
    return at is not None and since <= at <= until


def version_ref(function_arn: str, version: str) -> str:
    """A version's change ref, also `ActionPlan.current` / `desired` of an alias rollback."""
    return f"{function_arn.split(':function:')[0]}:function:{function_name(function_arn)}:{version}"


def function_name(function_arn: str) -> str:
    """my-fn from arn:aws:lambda:REGION:ACCOUNT:function:my-fn[:qualifier]."""
    return function_arn.split(":function:", 1)[-1].split(":", 1)[0]


def published(versions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Published versions (not $LATEST), oldest first."""
    numbered = [v for v in versions if str(v.get("Version", "")).isdigit()]
    return sorted(numbered, key=lambda v: int(v["Version"]))


def describe_function(
    config: dict[str, Any],
    versions: list[dict[str, Any]],
    aliases: list[dict[str, Any]],
    metrics: dict[str, rules.Points],
    since: datetime,
    until: datetime,
) -> ReaderOutput:
    name = str(config.get("FunctionName") or "function")
    arn = str(config.get("FunctionArn") or name)
    out = ReaderOutput()
    for v in published(versions):
        at = parse_time(v.get("LastModified"))
        if _in(at, since, until) and at is not None:
            out.lines.append(
                line(
                    at,
                    LAMBDA,
                    name,
                    f"version {v['Version']} published (code {str(v.get('CodeSha256'))[:12]}, "
                    f"runtime {v.get('Runtime')}, memory {v.get('MemorySize')} MB)",
                )
            )
            out.changes.append(
                ChangeMarker(
                    kind="deploy",
                    ref=version_ref(arn, str(v["Version"])),
                    at=at,
                    label=f"{name} version {v['Version']}",
                )
            )
    modified = parse_time(config.get("LastModified"))
    sha = str(config.get("CodeSha256") or "")[:12]
    if _in(modified, since, until) and modified is not None:
        out.lines.append(
            line(
                modified,
                LAMBDA,
                name,
                f"$LATEST updated: code {sha}, status {config.get('LastUpdateStatus')}",
            )
        )
        out.changes.append(
            ChangeMarker(
                kind="deploy",
                ref=f"{arn}:$LATEST@{sha}",
                at=modified,
                label=f"{name} code updated ({sha})",
            )
        )
    for alias in aliases:
        weights = ((alias.get("RoutingConfig") or {}).get("AdditionalVersionWeights")) or {}
        extra = ", ".join(f"{w:.0%} to version {v}" for v, w in weights.items())
        out.lines.append(
            line(
                until,
                LAMBDA,
                name,
                f"alias {alias.get('Name')} -> version {alias.get('FunctionVersion')}"
                + (f" ({extra})" if extra else ""),
            )
        )
    by_minute: dict[datetime, dict[str, float]] = {}
    for key, points in metrics.items():
        for t, value in points:
            by_minute.setdefault(t, {})[key] = value
    for t in sorted(by_minute):
        row = by_minute[t]
        if _in(t, since, until) and (row.get("errors") or row.get("throttles")):
            out.lines.append(
                line(
                    t,
                    LAMBDA,
                    name,
                    f"{row.get('errors', 0):.0f} errors, {row.get('throttles', 0):.0f} "
                    f"throttles in {row.get('invocations', 0):.0f} invocations this minute",
                )
            )
    out.lines.append(
        line(
            until,
            LAMBDA,
            name,
            f"now: state {config.get('State')}, runtime {config.get('Runtime')}, memory "
            f"{config.get('MemorySize')} MB, timeout {config.get('Timeout')} s, code {sha}",
        )
    )
    return out


def read_function(
    reader: AwsReader,
    region: str | None,
    function: str,
    log_group: str | None,
    since: datetime,
    until: datetime,
) -> ReaderOutput:
    """Blocking (boto3): call with asyncio.to_thread."""
    config = reader.call("lambda", region, "GetFunctionConfiguration", FunctionName=function)
    versions = reader.paginate(
        "lambda",
        region,
        "ListVersionsByFunction",
        "Versions",
        limit=MAX_VERSIONS,
        FunctionName=function,
    )
    aliases = reader.paginate("lambda", region, "ListAliases", "Aliases", FunctionName=function)
    metrics = rules.fetch_lambda(reader, region, function, until)
    out = describe_function(config, versions, aliases, metrics, since, until)
    if log_group:
        logs = cloudwatch_logs.read_log_group(reader, region, log_group, since, until)
        out.lines.extend(logs.lines)
        out.truncated = out.truncated or logs.truncated
    return out


def describe_instance(
    instance: dict[str, Any],
    status: dict[str, Any] | None,
    metrics: dict[str, rules.Points],
    since: datetime,
    until: datetime,
) -> ReaderOutput:
    iid = str(instance.get("InstanceId") or "instance")
    name = next((str(t["Value"]) for t in instance.get("Tags", []) if t.get("Key") == "Name"), iid)
    out = ReaderOutput()
    state = (instance.get("State") or {}).get("Name")
    launched = parse_time(instance.get("LaunchTime"))
    if _in(launched, since, until) and launched is not None:
        out.lines.append(
            line(launched, EC2, name, f"instance {iid} launched ({instance.get('ImageId')})")
        )
    reason = instance.get("StateTransitionReason")
    if reason:
        out.lines.append(line(until, EC2, name, f"last state change: {reason}"))
    signal = rules.ec2_signal(None, name, status, until)
    if signal is not None:
        out.lines.append(line(max(min(signal.at, until), since), EC2, name, signal.detail))
    for event in (status or {}).get("Events", []):
        at = parse_time(event.get("NotBefore")) or until
        out.lines.append(
            line(
                max(min(at, until), since),
                EC2,
                name,
                f"scheduled event {event.get('Code')}: {event.get('Description')}",
            )
        )
    for t, v in sorted(metrics.get("status_failed", [])):
        if _in(t, since, until) and v > 0:
            out.lines.append(line(t, EC2, name, "status check failed this minute"))
    cpu = [v for t, v in metrics.get("cpu", []) if _in(t, since, until)]
    out.lines.append(
        line(
            until,
            EC2,
            name,
            f"now: {iid} {state}, type {instance.get('InstanceType')}"
            + (f", CPU max {max(cpu):.0f}% in the window" if cpu else ""),
        )
    )
    return out


def fetch_ec2_metrics(
    reader: AwsReader, region: str | None, instance: str, since: datetime, until: datetime
) -> dict[str, rules.Points]:
    def query(metric_id: str, metric: str, stat: str) -> dict[str, Any]:
        return {
            "Id": metric_id,
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/EC2",
                    "MetricName": metric,
                    "Dimensions": [{"Name": "InstanceId", "Value": instance}],
                },
                "Period": 60,
                "Stat": stat,
            },
        }

    results = reader.call(
        "cloudwatch",
        region,
        "GetMetricData",
        MetricDataQueries=[
            query("cpu", "CPUUtilization", "Maximum"),
            query("status_failed", "StatusCheckFailed", "Maximum"),
        ],
        StartTime=since - timedelta(minutes=5),
        EndTime=until,
    ).get("MetricDataResults", [])
    return {
        str(r.get("Id")): [
            (t, float(v))
            for t, v in zip(r.get("Timestamps", []), r.get("Values", []), strict=False)
            if isinstance(t, datetime)
        ]
        for r in results
    }


def read_instance(
    reader: AwsReader, region: str | None, instance: str, since: datetime, until: datetime
) -> ReaderOutput:
    """Blocking (boto3): call with asyncio.to_thread."""
    reservations = reader.call("ec2", region, "DescribeInstances", InstanceIds=[instance]).get(
        "Reservations", []
    )
    found = [i for r in reservations for i in r.get("Instances", [])]
    if not found:
        return ReaderOutput(lines=[line(until, EC2, instance, "instance not found")])
    status = rules.fetch_ec2_status(reader, region, instance)
    metrics = fetch_ec2_metrics(reader, region, instance, since, until)
    return describe_instance(found[0], status, metrics, since, until)


ALB = "alb"
RDS = "rds"


def describe_balancer(
    name: str,
    config: dict[str, Any],
    metrics: dict[str, rules.Points],
    health: dict[str, list[dict[str, Any]]],
    since: datetime,
    until: datetime,
) -> ReaderOutput:
    """Per-minute 5xx and p95 where something is off, and each target group's target health
    now with the reason a target fails (Target.ResponseCodeMismatch, Target.Timeout, ...)."""
    out = ReaderOutput()
    by_minute: dict[datetime, dict[str, float]] = {}
    for key in ("requests", "target5xx", "elb5xx", "p95"):
        for t, value in metrics.get(key, []):
            by_minute.setdefault(t, {})[key] = value
    for t in sorted(by_minute):
        row = by_minute[t]
        errors = row.get("target5xx", 0) + row.get("elb5xx", 0)
        if _in(t, since, until) and (errors or row.get("p95", 0) >= rules.ALB_LATENCY_FLOOR_S):
            out.lines.append(
                line(
                    t,
                    ALB,
                    name,
                    f"{row.get('target5xx', 0):.0f} target 5xx, {row.get('elb5xx', 0):.0f} "
                    f"balancer 5xx in {row.get('requests', 0):.0f} requests, p95 "
                    f"{row.get('p95', 0):.2f} s this minute",
                )
            )
    for group in config.get("target_groups") or []:
        found = health.get(str(group.get("arn")), [])
        bad = [h for h in found if (h.get("TargetHealth") or {}).get("State") != "healthy"]
        text = f"target group {group.get('name')}: {len(found) - len(bad)}/{len(found)} healthy"
        text += f" (health check {group.get('health_check_path') or '-'})"
        for h in bad[:5]:
            th = h.get("TargetHealth") or {}
            text += (
                f"; {(h.get('Target') or {}).get('Id')} {th.get('State')} {th.get('Reason') or ''}"
                f" {th.get('Description') or ''}".rstrip()
            )
        out.lines.append(line(until, ALB, name, text))
    out.lines.append(
        line(until, ALB, name, f"now: {config.get('state') or '?'}, {config.get('dns_name') or ''}")
    )
    return out


def read_balancer(
    reader: AwsReader,
    region: str | None,
    name: str,
    config: dict[str, Any],
    since: datetime,
    until: datetime,
) -> ReaderOutput:
    """Blocking (boto3): call with asyncio.to_thread."""
    groups = [g for g in config.get("target_groups") or [] if g.get("dimension")]
    metrics = rules.fetch_alb(
        reader,
        region,
        str(config.get("dimension") or ""),
        [str(g["dimension"]) for g in groups],
        until,
    )
    health: dict[str, list[dict[str, Any]]] = {}
    for group in groups:
        health[str(group.get("arn"))] = reader.call(
            "elbv2", region, "DescribeTargetHealth", TargetGroupArn=group.get("arn")
        ).get("TargetHealthDescriptions", [])
    return describe_balancer(name, config, metrics, health, since, until)


def read_cluster(
    reader: AwsReader, region: str | None, cluster: str, since: datetime, until: datetime
) -> ReaderOutput:
    """Blocking (boto3). An Aurora cluster: status, members, pending changes, and CPU and read
    latency per minute (the DBClusterIdentifier dimension)."""
    found = reader.call("rds", region, "DescribeDBClusters", DBClusterIdentifier=cluster).get(
        "DBClusters", []
    )
    out = ReaderOutput()
    if not found:
        out.lines.append(line(until, RDS, cluster, "cluster not found"))
        return out
    c = found[0]
    cpu, latency = rules.fetch_rds(reader, region, cluster, until, "DBClusterIdentifier")
    lat = dict(latency)
    for t, value in cpu:
        if _in(t, since, until):
            out.lines.append(
                line(t, RDS, cluster, f"CPU {value:.0f}%, read latency {lat.get(t, 0):.1f} ms")
            )
    members = ", ".join(
        f"{m.get('DBInstanceIdentifier')}{' (writer)' if m.get('IsClusterWriter') else ''}"
        for m in c.get("DBClusterMembers", [])
    )
    pending = c.get("PendingModifiedValues") or {}
    out.lines.append(
        line(
            until,
            RDS,
            cluster,
            f"now: {c.get('Status')}, {c.get('Engine')} {c.get('EngineVersion')}, members "
            f"{members or '-'}" + (f", pending {pending}" if pending else ""),
        )
    )
    return out
