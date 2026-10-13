"""RDS reader: CloudWatch metrics for one DB instance, spikes marked.

CPUUtilization, DatabaseConnections, ReadLatency, and ReadIOPS at one-minute resolution. A point
is a spike when it is at least twice the window's baseline (the median of its first half) and
over an absolute floor per metric: on an idle database the baseline is near 0, and 2.4 ms read
latency against 0.0 ms is not a spike. Every spike is kept; other points are sampled so a slow
climb still shows.
"""

import statistics
from datetime import datetime
from typing import Any

from app.connectors.aws import AwsReader
from app.telemetry.evidence import Line, ReaderOutput, line, parse_time

SOURCE = "rds"
PERIOD_SECONDS = 60
SAMPLE_EVERY = 5

# metric id, CloudWatch name, unit label, value scale, spike floor (in the label's unit)
METRICS: tuple[tuple[str, str, str, float, float], ...] = (
    ("cpu", "CPUUtilization", "%", 1.0, 30.0),
    ("connections", "DatabaseConnections", "", 1.0, 5.0),
    ("read_latency", "ReadLatency", "ms", 1000.0, 20.0),
    ("read_iops", "ReadIOPS", "/s", 1.0, 50.0),
)


def queries(db_identifier: str) -> list[dict[str, Any]]:
    return [
        {
            "Id": metric_id,
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/RDS",
                    "MetricName": name,
                    "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": db_identifier}],
                },
                "Period": PERIOD_SECONDS,
                "Stat": "Average",
            },
            "ReturnData": True,
        }
        for metric_id, name, _, _, _ in METRICS
    ]


def _fmt(value: float, unit: str) -> str:
    return f"{value:.1f}{unit}" if value < 100 else f"{value:.0f}{unit}"


def metric_lines(
    db: str, metric_id: str, points: list[tuple[datetime, float]], unit: str, floor: float
) -> list[Line]:
    if not points:
        return []
    points.sort()
    values = [v for _, v in points]
    baseline = statistics.median(values[: max(1, len(values) // 2)])
    name = next(m[1] for m in METRICS if m[0] == metric_id)
    out = []
    for i, (at, value) in enumerate(points):
        spike = value >= max(2 * baseline, floor) and value - baseline >= floor / 2
        if spike or i % SAMPLE_EVERY == 0 or i == len(points) - 1:
            text = f"{name} {_fmt(value, unit)} (baseline {_fmt(baseline, unit)})"
            if spike:
                text += " [SPIKE]"
            out.append(line(at, SOURCE, db, text))
    return out


def lines_from_results(db: str, results: list[dict[str, Any]]) -> list[Line]:
    out: list[Line] = []
    by_id = {r.get("Id"): r for r in results}
    for metric_id, _, unit, scale, floor in METRICS:
        result = by_id.get(metric_id) or {}
        points = [
            (at, float(v) * scale)
            for t, v in zip(result.get("Timestamps", []), result.get("Values", []), strict=False)
            if (at := parse_time(t)) is not None
        ]
        out.extend(metric_lines(db, metric_id, points, unit, floor))
    return out


def read_db(
    reader: AwsReader,
    region: str | None,
    db_identifier: str,
    since: datetime,
    until: datetime,
) -> ReaderOutput:
    """Blocking (boto3): call with asyncio.to_thread."""
    results = reader.paginate(
        "cloudwatch",
        region,
        "GetMetricData",
        "MetricDataResults",
        limit=len(METRICS) * 10,
        MetricDataQueries=queries(db_identifier),
        StartTime=since,
        EndTime=until,
        ScanBy="TimestampAscending",
    )
    # Paginated results repeat an Id per page; merge them before computing baselines.
    merged: dict[str, dict[str, list[Any]]] = {}
    for r in results:
        entry = merged.setdefault(str(r.get("Id")), {"Timestamps": [], "Values": []})
        entry["Timestamps"].extend(r.get("Timestamps", []))
        entry["Values"].extend(r.get("Values", []))
    flat = [{"Id": k, **v} for k, v in merged.items()]
    return ReaderOutput(lines=lines_from_results(db_identifier, flat))
