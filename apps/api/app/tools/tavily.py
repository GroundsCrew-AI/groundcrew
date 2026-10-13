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
        out = []
        for item in response.json().get("results", [])[:max_results]:
            url = str(item.get("url") or "")
            if not url.startswith(("https://", "http://")):
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


def _json_message(message: str) -> str:
    """For a JSON log line, its message and error text (the first line of a traceback); the
    other fields (ts, level, service, ...) only make a search worse."""
    try:
        data = json.loads(message)
    except ValueError:
        return message
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


# Reader notes and request summaries are Groundcrew's lines, not the application's errors.
_NOT_APP = ("latency summary", "reader failed", "reader timed out")


def dominant_error(lines: Iterable[str]) -> str:
    """The query for the application's most frequent error. Lines that differ only in ids,
    times and numbers are the same error. Empty when no line is an error."""
    counts: Counter[str] = Counter()
    for line in lines:
        message = line.split(" | ")[-1].strip()
        if message.startswith(_NOT_APP) or not is_error_line(message):
            continue
        query = search_query(message)
        if query:
            counts[query] += 1
    return counts.most_common(1)[0][0] if counts else ""
