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

The readers land in M1. M0 fixes the shape and gives Medic's tests a fake.
"""

import uuid
from datetime import datetime
from typing import Protocol

LINE_SEPARATOR = " | "


def evidence_line(at: datetime, source: str, resource: str, message: str) -> str:
    """One evidence line. Newlines in the message are folded so a line stays one line."""
    text = " ".join(message.split())
    return LINE_SEPARATOR.join((at.isoformat(), source, resource, text))


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
