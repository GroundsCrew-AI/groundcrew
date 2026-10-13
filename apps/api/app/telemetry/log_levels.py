"""Is a log line an error? By its level, never by its logger's name.

uvicorn logs its startup and shutdown under the logger "uvicorn.error" at INFO ("Started server
process", "Waiting for application shutdown."), so every deploy looked like an error spike when
lines were matched on the word "error". Now:

1. A JSON line with a level field (`level`, `levelname`, `severity`, ...) is an error when the
   level is error/critical/fatal, or it carries a traceback, or its HTTP status is 5xx.
2. A text line that starts with a level (`INFO:     ...`, `2026-10-13 10:39:50 WARNING ...`)
   is classified the same way.
3. Only a line with no level falls back to error words, and a word inside a dotted name
   (`uvicorn.error`, `app.errors`) does not count.
"""

import json
import re
from typing import Any

ERROR_LEVELS = frozenset(
    {"error", "err", "critical", "crit", "fatal", "alert", "emerg", "emergency", "panic", "severe"}
)
_LEVEL_KEYS = ("level", "levelname", "severity", "log.level", "loglevel", "lvl")
_TRACE_KEYS = ("exc", "exc_info", "exception", "stack", "stack_trace", "traceback", "error")
_STATUS_KEYS = ("status", "status_code", "http_status")
TRACEBACK = re.compile(
    r"Traceback \(most recent call last\)|^\s+File \"|^\s+at \S+\(|"
    r"^\w+(\.\w+)*(Error|Exception):|^panic: |^goroutine \d+ \[",
    re.MULTILINE,
)
# A level near the start, upper case ("INFO:     Started", "2026-10-13 10:39:50 [ERROR] x"),
# or logfmt anywhere ("level=info"); a lower-case "info" in a sentence is not a level.
_TEXT_LEVEL = re.compile(
    r"^\W{0,2}(?:\S+\s+){0,3}?\[?(DEBUG|TRACE|INFO|NOTICE|WARN|WARNING|ERROR|ERR|CRITICAL|FATAL)"
    r"\]?(?=[:\s\]])|\blevel=\"?([A-Za-z]+)"
)
# An HTTP 5xx in an access line ("GET /orders/search HTTP/1.1" 500).
_HTTP_5XX = re.compile(r"HTTP/[\d.]+\"?\s+5\d\d\b|\s5\d\d\s")
# Not preceded or followed by a dot or word character: "uvicorn.error" is a name, not an error.
DEFAULT_WORDS = re.compile(
    r"(?i)(?<![\w.])(error|errors|exception|traceback|fatal|critical|failed|failure|"
    r"unreachable|refused|timed? ?out|killed|outofmemory|panic)(?![\w.])"
)


def _json(message: str) -> dict[str, Any] | None:
    text = message.strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _status_5xx(data: dict[str, Any]) -> bool:
    for key in _STATUS_KEYS:
        value = data.get(key)
        if isinstance(value, int) and 500 <= value <= 599:
            return True
        if isinstance(value, str) and value.isdigit() and 500 <= int(value) <= 599:
            return True
    return False


def _has_trace(data: dict[str, Any]) -> bool:
    return any(isinstance(data.get(k), str) and TRACEBACK.search(str(data[k])) for k in _TRACE_KEYS)


def is_error_line(message: str, words: re.Pattern[str] = DEFAULT_WORDS) -> bool:
    data = _json(message)
    if data is not None:
        level = next((data[k] for k in _LEVEL_KEYS if isinstance(data.get(k), str)), None)
        if level is not None:
            return (
                level.strip().lower() in ERROR_LEVELS
                or _has_trace(data)
                or _status_5xx(data)
                or bool(_HTTP_5XX.search(str(data.get("msg") or data.get("message") or "")))
            )
        text = " ".join(str(v) for v in data.values() if isinstance(v, str))
        return _status_5xx(data) or _has_trace(data) or bool(words.search(text))
    found = _TEXT_LEVEL.search(message)
    if found is not None:
        level = (found.group(1) or found.group(2)).lower()
        return (
            level in ERROR_LEVELS
            or bool(TRACEBACK.search(message))
            or bool(_HTTP_5XX.search(message))
        )
    return bool(words.search(message) or TRACEBACK.search(message) or _HTTP_5XX.search(message))
