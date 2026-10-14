"""Tavily web search for Medic's research step (the hackathon's Tavily bonus needs a real call).

One POST to /search per query, basic depth, at most 3 results kept with their URLs. The key
comes from TAVILY_API_KEY and is never logged; error text is redacted. Medic caps calls per
incident (TAVILY_MAX_CALLS_PER_INCIDENT). Tests use a MockTransport, never the API.
"""

import json
import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

import httpx
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.security.redact import redact
from app.telemetry.log_levels import is_error_line

TAVILY_URL = "https://api.tavily.com/search"
MAX_RESULTS = 3
SNIPPET_CHARS = 400
TIMEOUT_SECONDS = 20.0


class TavilyError(Exception):
    pass


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str


class TavilyClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        key = self.settings.tavily_api_key
        if key is None or not key.get_secret_value():
            raise TavilyError("TAVILY_API_KEY is not set.")
        self._key = key.get_secret_value()
        self._transport = transport

    async def search(self, query: str, max_results: int = MAX_RESULTS) -> list[SearchResult]:
        body: dict[str, Any] = {
            "query": query[:400],
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": False,
        }
        async with httpx.AsyncClient(transport=self._transport, timeout=TIMEOUT_SECONDS) as client:
            try:
                response = await client.post(
                    TAVILY_URL,
                    json=body,
                    headers={"Authorization": f"Bearer {self._key}", "User-Agent": "groundcrew"},
                )
            except httpx.HTTPError as exc:
                raise TavilyError(f"Tavily search failed: {type(exc).__name__}") from None
        if response.status_code >= 400:
            text = response.text[:200].replace(self._key, "[REDACTED]")
            raise TavilyError(redact(f"Tavily search returned {response.status_code}: {text}"))
        out: list[SearchResult] = []
        for item in response.json().get("results", [])[:max_results]:
            url = str(item.get("url") or "")
            # Tavily can return the same page twice (two chunks of it); keep the first.
            if not url.startswith(("https://", "http://")) or any(r.url == url for r in out):
                continue
            snippet = " ".join(str(item.get("content") or "").split())[:SNIPPET_CHARS]
            out.append(
                SearchResult(title=str(item.get("title") or url)[:200], url=url, snippet=snippet)
            )
        return out


_IDS = re.compile(
    r"\b[0-9a-f]{7,64}\b|\b\d{4}-\d\d-\d\dT[\d:.+Z-]+|\b\d+(\.\d+)?(ms|s|%)?\b|arn:\S+|\(task \S+\)"
)


_MESSAGE_KEYS = ("msg", "message", "event")
_ERROR_KEYS = ("error", "err", "exc", "exception", "error_message", "reason")


_JSON_MESSAGE = re.compile(r'"(?:msg|message|event)"\s*:\s*"((?:[^"\\]|\\.)*)"')
# The exception at the end of a traceback, written into a JSON string ("...\nKeyError: 'x'").
_EXCEPTION = re.compile(r"(?:^|\\n|\s)([A-Za-z_][\w.]*(?:Error|Exception|Timeout)\b(?::[^\"\\]*)?)")


def _broken_json_message(message: str) -> str:
    """A JSON log line cut short (evidence lines are truncated, a traceback rarely fits): its
    message and the last exception named in it, found by pattern."""
    parts = [m.group(1) for m in _JSON_MESSAGE.finditer(message)][:1]
    exceptions = [m.group(1).strip() for m in _EXCEPTION.finditer(message)]
    if exceptions:
        parts.append(exceptions[-1])
    return " ".join(parts)


def _json_message(message: str) -> str:
    """For a JSON log line, its message and error text (the first line of a traceback); the
    other fields (ts, level, service, ...) only make a search worse."""
    try:
        data = json.loads(message)
    except ValueError:
        return _broken_json_message(message)
    if not isinstance(data, dict):
        return message
    parts = []
    for key in (*_MESSAGE_KEYS, *_ERROR_KEYS):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            # A traceback ends with the exception; that is the searchable part.
            lines = [x for x in value.strip().splitlines() if x.strip()]
            parts.append(lines[-1] if key in _ERROR_KEYS else lines[0])
    return " ".join(dict.fromkeys(parts))


def search_query(line: str) -> str:
    """A web query from an evidence line: the message without ids, times, and numbers that
    would make every search unique."""
    message = line.split(" | ")[-1].strip()
    if message.startswith("{"):
        message = _json_message(message)
    return " ".join(_IDS.sub(" ", message).split())[:200]


# Words that name the application's stack, by what they imply (asyncpg means Postgres).
_STACK: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("postgres", ("postgres", "postgresql", "asyncpg", "psycopg", "psycopg2", "pgbouncer")),
    ("asyncpg", ("asyncpg",)),
    ("psycopg", ("psycopg", "psycopg2")),
    ("sqlalchemy", ("sqlalchemy",)),
    ("mysql", ("mysql", "pymysql", "mysqlclient", "mariadb")),
    ("redis", ("redis",)),
    ("mongodb", ("mongodb", "pymongo", "mongoose")),
    ("django", ("django",)),
    ("fastapi", ("fastapi",)),
    ("flask", ("flask",)),
    ("prisma", ("prisma",)),
)
# Databases a result can be about instead of ours (SQL Server articles for a Postgres app).
_DATABASES: dict[str, tuple[str, ...]] = {
    "postgres": ("postgres", "postgresql", "asyncpg", "psycopg", "npgsql"),
    "mysql": ("mysql", "mariadb"),
    "sqlserver": ("sql server", "sqlserver", "mssql", "t-sql", "azure sql"),
    "oracle": ("oracle",),
    "mongodb": ("mongodb", "mongo "),
}
_WORD = re.compile(r"[a-z][a-z0-9_]+")
# Too common in error text to tell two errors apart.
_GENERIC = frozenset(
    """
    error errors failed failure exception traceback most recent call last file line
    none with from that this when connection connect database request response server
    client invalid timeout timed could cannot unable while true false
    """.split()  # noqa: SIM905
)


def stack_terms(lines: Iterable[str], limit: int = 3) -> list[str]:
    """The application's stack named in its own lines (tracebacks, log fields), most specific
    last: e.g. ["postgres", "asyncpg", "sqlalchemy"]."""
    words = set(_WORD.findall(" ".join(lines).lower()))
    found = [name for name, aliases in _STACK if words & set(aliases)]
    return found[:limit]


def with_stack(query: str, stack: list[str]) -> str:
    """The query with the stack terms it does not already have ("... postgres asyncpg")."""
    have = set(_WORD.findall(query.lower()))
    extra = [t for t in stack if t not in have]
    return " ".join([query, *extra]).strip()[:250]


def relevant(results: list[SearchResult], query: str, stack: list[str]) -> list[SearchResult]:
    """Results about this error: they share a distinctive term with the query or name the
    stack, and are not about another database than the application's."""
    ours = next((db for db in _DATABASES if db in stack), None)
    terms = {w for w in _WORD.findall(query.lower()) if len(w) >= 4 and w not in _GENERIC}
    keep = []
    for r in results:
        text = f"{r.title} {r.snippet} {r.url}".lower()
        if ours is not None and not any(a in text for a in _DATABASES[ours]):
            others = (db for db in _DATABASES if db != ours)
            if any(a in text for db in others for a in _DATABASES[db]):
                continue
        words = set(_WORD.findall(text))
        if terms & words or set(stack) & words:
            keep.append(r)
    return keep


_NAMED_EXCEPTION = re.compile(r"\b[A-Z][A-Za-z]*(?:Error|Exception)\b")

# Reader notes and request summaries are Groundcrew's lines, not the application's errors.
_NOT_APP = ("latency summary", "reader failed", "reader timed out")


def dominant_error(lines: Iterable[str]) -> str:
    """The query for the application's most frequent error. Lines that differ only in ids,
    times and numbers are the same error. Empty when no line is an error."""
    counts: Counter[str] = Counter()
    for line in lines:
        message = line.split(" | ")[-1].strip()
        named = bool(_NAMED_EXCEPTION.search(message))
        if message.startswith(_NOT_APP) or not (named or is_error_line(message)):
            continue
        query = search_query(message)
        if query:
            counts[query] += 1
    if not counts:
        return ""
    # Most frequent first; on a tie, the line that names an exception (OutOfMemoryError) says
    # more than a status ("rollout FAILED").
    return max(counts, key=lambda q: (counts[q], bool(_NAMED_EXCEPTION.search(q))))
