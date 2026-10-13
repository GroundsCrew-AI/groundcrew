"""CloudWatch Logs reader: FilterLogEvents on one log group over the window.

Keeps what explains an incident: error-looking lines and tracebacks with one line of context on
each side, then a sample of the rest when there are few errors. Request logs that carry a
latency field (`latency_ms`, `duration_ms`, `elapsed_ms`) and a path are also summarized per
minute and path, so a slow endpoint shows as one line per minute instead of thousands of
requests. A minute whose p50 is SLOW_FACTOR x the path's baseline (and over SLOW_FLOOR_MS) is
marked [SLOW]; health-check paths are summarized but never marked (their p95 jumps without a
problem).

Busy log groups have more events than MAX_EVENTS: `read_newest` reads the window newest first,
so what is cut is the start of the window, not the minutes around the incident.
"""

import json
import re
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from app.connectors.aws import AwsReader
from app.telemetry.evidence import MAX_LINES_PER_SOURCE, Line, ReaderOutput, line, parse_time
from app.telemetry.log_levels import is_error_line

SOURCE = "cloudwatch_logs"
MAX_EVENTS = 5_000
CONTEXT = 1
# Error words for lines with no level (log_levels.is_error_line); not inside dotted names.
_ERROR = re.compile(
    r"(?i)(?<![\w.])(error|errors|exception|traceback|fatal|critical|failed|failure|unreachable|refused|"
    r"timed? ?out|killed|oom|outofmemory|panic|denied|cannot|could not)(?![\w.])"
)
_TRACE = re.compile(r'^\s+(File "|at |\.\.\.)|^\w+(\.\w+)*(Error|Exception):')
_LATENCY_KEYS = ("latency_ms", "duration_ms", "elapsed_ms")
_LATENCY_TEXT = re.compile(r"\b(latency_ms|duration_ms|elapsed_ms)[=:]\s*([0-9.]+)")
_PATH_TEXT = re.compile(r"\bpath[=:]\s*(\S+)")
# Load balancer and container health checks: summarized, never a latency signal.
HEALTH_PATH = re.compile(r"(?i)^/(health|healthz|healthcheck|ready|readyz|live|livez|ping)\b")
SLOW_FACTOR = 3.0
SLOW_FLOOR_MS = 20.0
MIN_REQUESTS = 3
# Newest first, one slice at a time (FilterLogEvents returns oldest first).
READ_SLICE = timedelta(minutes=2)
MIN_SLICE = timedelta(seconds=5)


def _fields(message: str) -> dict[str, Any]:
    text = message.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def request_latency(message: str) -> tuple[str, float] | None:
    """(path, milliseconds) when the line is a request log with a latency field and a path."""
    data = _fields(message)
    path, value = "", None
    for key in _LATENCY_KEYS:
        if isinstance(data.get(key), int | float):
            path = str(data.get("path") or data.get("route") or data.get("url") or "")
            value = float(data[key])
            break
    else:
        match = _LATENCY_TEXT.search(message)
        if match:
            found = _PATH_TEXT.search(message)
            path, value = (found.group(1) if found else ""), float(match.group(2))
    path = path.strip("\"',")
    if "://" in path:
        path = "/" + path.split("://", 1)[1].partition("/")[2]
    path = path.split("?", 1)[0]
    if value is None or not path.startswith("/"):
        return None
    return path, value


def is_health(path: str) -> bool:
    return bool(HEALTH_PATH.match(path))


def minute_p50s(
    events: list[tuple[datetime, str]],
) -> dict[tuple[datetime, str], tuple[int, float]]:
    """(minute, path) -> (requests, p50 ms) over request logs."""
    buckets: dict[tuple[datetime, str], list[float]] = defaultdict(list)
    for at, message in events:
        found = request_latency(message)
        if found:
            buckets[(at.replace(second=0, microsecond=0), found[0])].append(found[1])
    return {k: (len(v), statistics.median(v)) for k, v in buckets.items()}


def slow_minutes(
    p50s: dict[tuple[datetime, str], tuple[int, float]], baseline_until: datetime | None = None
) -> dict[tuple[datetime, str], float]:
    """(minute, path) -> baseline p50, for minutes at SLOW_FACTOR x the path's baseline and over
    SLOW_FLOOR_MS, with MIN_REQUESTS. A path's baseline is the median minute p50 before
    `baseline_until` (default: the first half of the minutes seen). Health paths never count."""
    by_path: dict[str, list[tuple[datetime, int, float]]] = defaultdict(list)
    for (minute, path), (count, p50) in p50s.items():
        if not is_health(path):
            by_path[path].append((minute, count, p50))
    out: dict[tuple[datetime, str], float] = {}
    for path, points in by_path.items():
        points.sort()
        if baseline_until is None:
            before = [p50 for _, _, p50 in points[: max(1, len(points) // 2)]]
        else:
            before = [p50 for minute, _, p50 in points if minute < baseline_until]
        if not before:
            continue
        base = statistics.median(before)
        for minute, count, p50 in points:
            if count >= MIN_REQUESTS and p50 >= max(SLOW_FLOOR_MS, SLOW_FACTOR * base):
                out[(minute, path)] = base
    return out


def _is_error(message: str) -> bool:
    # A traceback's continuation lines are their own events in plain-text logs.
    return is_error_line(message, _ERROR) or bool(_TRACE.search(message))


def latency_summaries(events: list[tuple[datetime, str]], resource: str) -> list[Line]:
    """One line per minute and path: request count, p50, p95, max; [SLOW] minutes marked."""
    buckets: dict[tuple[datetime, str], list[float]] = defaultdict(list)
    for at, message in events:
        found = request_latency(message)
        if found:
            minute = at.replace(second=0, microsecond=0)
            buckets[(minute, found[0])].append(found[1])
    slow = slow_minutes({k: (len(v), statistics.median(v)) for k, v in buckets.items()})
    out = []
    for (minute, path), values in sorted(buckets.items()):
        values.sort()
        p95 = values[min(len(values) - 1, int(len(values) * 0.95))]
        text = (
            f"latency summary path={path} requests={len(values)} "
            f"p50={statistics.median(values):.0f}ms p95={p95:.0f}ms max={values[-1]:.0f}ms"
        )
        if (minute, path) in slow:
            text += f" (baseline p50 {slow[(minute, path)]:.0f}ms) [SLOW]"
        out.append(line(minute, SOURCE, resource, text))
    return out


def select(events: list[tuple[datetime, str]], resource: str) -> ReaderOutput:
    """Errors with context, latency summaries, then a sample of other lines to fill up."""
    events = sorted(events, key=lambda e: e[0])
    keep: set[int] = set()
    for i, (_, message) in enumerate(events):
        if _is_error(message):
            keep.update(range(max(0, i - CONTEXT), min(len(events), i + CONTEXT + 1)))
    summaries = latency_summaries(events, resource)
    request_logs = {i for i, (_, m) in enumerate(events) if request_latency(m) and i not in keep}
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
    events, read_from = read_newest(reader, region, log_group, since, until, MAX_EVENTS)
    out = select(events, log_group)
    if read_from is not None:
        out.truncated = True
        out.lines.append(
            line(
                read_from,
                SOURCE,
                log_group,
                f"read the newest {len(events)} events only; events from {since:%H:%M} to "
                f"{read_from:%H:%M:%S} UTC were not read",
            )
        )
    return out


def read_newest(
    reader: AwsReader,
    region: str | None,
    log_group: str,
    since: datetime,
    until: datetime,
    limit: int,
) -> tuple[list[tuple[datetime, str]], datetime | None]:
    """Blocking (boto3). At most `limit` events of the window, the newest, oldest first; and
    when the limit cut the window, the time from which the events read are complete (else None).

    Slices of READ_SLICE from `until` backwards; a slice with more events than the room left is
    split in halves, newer half first, down to MIN_SLICE."""
    out: list[tuple[datetime, str]] = []

    def read(start: datetime, end: datetime) -> list[tuple[datetime, str]] | None:
        """The slice's events, or None when there are more than the room left."""
        room = limit - len(out)
        raw = reader.paginate(
            "logs",
            region,
            "FilterLogEvents",
            "events",
            limit=room + 1,
            logGroupName=log_group,
            startTime=int(start.timestamp() * 1000),
            endTime=int(end.timestamp() * 1000) - 1,
        )
        if len(raw) > room:
            return None
        return sorted(
            (at, str(e.get("message", "")))
            for e in raw
            if (at := parse_time(e.get("timestamp"))) is not None and start <= at < end
        )

    pending = [(max(since, until - READ_SLICE), until)]
    while pending:
        start, end = pending.pop()
        if len(out) >= limit:
            return out, end
        chunk = read(start, end)
        if chunk is None:
            if end - start <= MIN_SLICE:
                return out, end
            middle = start + (end - start) / 2
            pending += [(start, middle), (middle, end)]  # newer half is popped first
            continue
        out[:0] = chunk
        if start > since and not pending:
            pending.append((max(since, start - READ_SLICE), start))
    return out, None
