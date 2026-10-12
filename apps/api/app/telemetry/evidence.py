"""Evidence for an incident (Phase 5, docs/phase5-interfaces.md).

`collect_evidence(workspace_id, resource_ids, since, until)` returns plain text per source:

    {"ecs:checkout-worker": "2026-10-19T10:02:11+00:00 | ecs | checkout-worker | ...\\n...",
     "cloudwatch_logs:/ecs/checkout-worker": "...",
     "secrets:checkout/database-url": "..."}

- Keys are `<reader>:<resource>`; readers are cloudwatch_logs, ecs, rds, secrets, github,
  vercel, and `timeline` (all sources merged in time order).
- Every line is `ISO-timestamp | source | resource | message`, oldest first, redacted, and
  each source is capped (a `... N more lines` marker says what was cut).
- Read-only: AWS through `CredentialsProvider.aws_session()` only; never a secret value or
  customer data (DB rows, S3 objects).

Each reader (cloudwatch_logs, ecs, rds, secrets, github, vercel) returns a `ReaderOutput`: its
`Line`s and the `ChangeMarker`s it saw (deploys, commits, secret changes). `render()` turns
lines into the text above; `app.telemetry.collect` picks readers from the graph.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from app.agents.medic.events import ChangeMarker
from app.security.redact import redact

LINE_SEPARATOR = " | "
# Per source, after selection. Medic ranks across sources afterwards.
MAX_LINES_PER_SOURCE = 150
MAX_MESSAGE_CHARS = 500


def evidence_line(at: datetime, source: str, resource: str, message: str) -> str:
    """One evidence line. Newlines in the message are folded so a line stays one line."""
    text = " ".join(message.split())
    return LINE_SEPARATOR.join((at.isoformat(), source, resource, text))


def utc(at: datetime) -> datetime:
    """Timezone-aware UTC (AWS and GitHub sometimes return naive or offset times)."""
    return at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)


def parse_time(value: object) -> datetime | None:
    """A datetime from an ISO string, epoch milliseconds, or a datetime; None otherwise."""
    if isinstance(value, datetime):
        return utc(value)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value / 1000, UTC)
    if isinstance(value, str) and value:
        try:
            return utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


@dataclass(frozen=True, order=True)
class Line:
    at: datetime
    source: str
    resource: str
    message: str

    def text(self) -> str:
        return evidence_line(self.at, self.source, self.resource, self.message)


def line(at: datetime, source: str, resource: str, message: str) -> Line:
    """A redacted, length-capped line."""
    text = redact(" ".join(message.split()))
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[: MAX_MESSAGE_CHARS - 3] + "..."
    return Line(utc(at), source, resource, text)


@dataclass
class ReaderOutput:
    lines: list[Line] = field(default_factory=list)
    changes: list[ChangeMarker] = field(default_factory=list)
    # True when the reader dropped lines (its own cap, or the API's page limit).
    truncated: bool = False


def cap(lines: list[Line], limit: int = MAX_LINES_PER_SOURCE) -> tuple[list[Line], int]:
    """Oldest first, at most `limit`: keeps the newest lines (closest to the incident) and
    returns how many were dropped."""
    ordered = sorted(set(lines))
    dropped = max(len(ordered) - limit, 0)
    return ordered[dropped:], dropped


def render(lines: list[Line], limit: int = MAX_LINES_PER_SOURCE) -> str:
    kept, dropped = cap(lines, limit)
    text = "\n".join(item.text() for item in kept)
    if dropped:
        text = f"... {dropped} more lines before these\n{text}"
    return text


class EvidenceCollector(Protocol):
    async def __call__(
        self,
        workspace_id: uuid.UUID,
        resource_ids: list[uuid.UUID],
        since: datetime,
        until: datetime,
    ) -> dict[str, str]: ...


class FakeEvidence:
    """Returns fixed text per source, whatever is asked. For Medic's tests."""

    def __init__(self, sources: dict[str, str]) -> None:
        self.sources = sources
        self.calls: list[tuple[uuid.UUID, list[uuid.UUID], datetime, datetime]] = []

    async def __call__(
        self,
        workspace_id: uuid.UUID,
        resource_ids: list[uuid.UUID],
        since: datetime,
        until: datetime,
    ) -> dict[str, str]:
        self.calls.append((workspace_id, list(resource_ids), since, until))
        return dict(self.sources)
