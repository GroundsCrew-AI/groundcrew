"""CloudWatch Logs reader: FilterLogEvents on one log group over the window.

Keeps what explains an incident: error-looking lines and tracebacks with one line of context on
each side, then a sample of the rest when there are few errors. Request logs that carry a
latency field (`latency_ms`, `duration_ms`, `elapsed_ms`) are also summarized per minute and
path, so a slow endpoint shows as one line per minute instead of thousands of requests.
"""

import json
import re
import statistics
from collections import defaultdict
from datetime import datetime
from typing import Any

from app.connectors.aws import AwsReader
from app.telemetry.evidence import MAX_LINES_PER_SOURCE, Line, ReaderOutput, line, parse_time

SOURCE = "cloudwatch_logs"
MAX_EVENTS = 5_000
CONTEXT = 1
_ERROR = re.compile(
    r"(?i)\b(error|exception|traceback|fatal|critical|failed|failure|unreachable|refused|"
    r"timed? ?out|killed|oom|outofmemory|panic|denied|cannot|could not)\b|\s5\d\d\s"
)
_TRACE = re.compile(r'^\s+(File "|at |\.\.\.)|^\w+(\.\w+)*(Error|Exception):')
_LATENCY_KEYS = ("latency_ms", "duration_ms", "elapsed_ms")
_LATENCY_TEXT = re.compile(r"\b(latency_ms|duration_ms|elapsed_ms)[=:]\s*([0-9.]+)")
_PATH_TEXT = re.compile(r"\bpath[=:]\s*(\S+)")


def _fields(message: str) -> dict[str, Any]:
    text = message.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _latency(message: str) -> tuple[str, float] | None:
    """(path, milliseconds) when the line is a request log with a latency field."""
    data = _fields(message)
    for key in _LATENCY_KEYS:
        value = data.get(key)
        if isinstance(value, int | float):
            path = str(data.get("path") or data.get("route") or data.get("url") or "?")
            return path.split("?", 1)[0], float(value)
    match = _LATENCY_TEXT.search(message)
    if match:
        found = _PATH_TEXT.search(message)
        return (found.group(1).split("?", 1)[0] if found else "?"), float(match.group(2))
    return None


def _is_error(message: str) -> bool:
    data = _fields(message)
    level = str(data.get("level") or data.get("levelname") or "").lower()
    if level in ("error", "critical", "fatal"):
        return True
    status = data.get("status") or data.get("status_code")
    if isinstance(status, int) and status >= 500:
        return True
    return bool(_ERROR.search(message) or _TRACE.search(message))


def latency_summaries(events: list[tuple[datetime, str]], resource: str) -> list[Line]:
    """One line per minute and path: request count, p50, p95, max."""
    buckets: dict[tuple[datetime, str], list[float]] = defaultdict(list)
    for at, message in events:
        found = _latency(message)
        if found:
            minute = at.replace(second=0, microsecond=0)
            buckets[(minute, found[0])].append(found[1])
    out = []
    for (minute, path), values in sorted(buckets.items()):
        values.sort()
        p95 = values[min(len(values) - 1, int(len(values) * 0.95))]
        out.append(
            line(
                minute,
                SOURCE,
                resource,
                f"latency summary path={path} requests={len(values)} "
                f"p50={statistics.median(values):.0f}ms p95={p95:.0f}ms max={values[-1]:.0f}ms",
            )
        )
    return out


def select(events: list[tuple[datetime, str]], resource: str) -> ReaderOutput:
    """Errors with context, latency summaries, then a sample of other lines to fill up."""
    events = sorted(events, key=lambda e: e[0])
    keep: set[int] = set()
    for i, (_, message) in enumerate(events):
        if _is_error(message):
            keep.update(range(max(0, i - CONTEXT), min(len(events), i + CONTEXT + 1)))
    summaries = latency_summaries(events, resource)
    request_logs = {i for i, (_, m) in enumerate(events) if _latency(m) and i not in keep}
    room = MAX_LINES_PER_SOURCE - len(keep) - len(summaries)
    rest = [i for i in range(len(events)) if i not in keep and i not in request_logs]
    if room > 0 and rest:
        step = max(1, len(rest) // room)
        keep.update(rest[::step][:room])
    lines = [line(events[i][0], SOURCE, resource, events[i][1]) for i in sorted(keep)]
    truncated = len(keep) + len(request_logs) < len(events)
    return ReaderOutput(lines=lines + summaries, truncated=truncated)


def read_log_group(
    reader: AwsReader,
    region: str | None,
    log_group: str,
    since: datetime,
    until: datetime,
) -> ReaderOutput:
    """Blocking (boto3): call with asyncio.to_thread."""
    raw = reader.paginate(
        "logs",
        region,
        "FilterLogEvents",
        "events",
        limit=MAX_EVENTS,
        logGroupName=log_group,
        startTime=int(since.timestamp() * 1000),
        endTime=int(until.timestamp() * 1000),
    )
    events = [
        (at, str(e.get("message", "")))
        for e in raw
        if (at := parse_time(e.get("timestamp"))) is not None
    ]
    out = select(events, log_group)
    out.truncated = out.truncated or len(raw) >= MAX_EVENTS
    return out
