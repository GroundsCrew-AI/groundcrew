"""Incident memory: similar resolved incidents, and remembering this one when it resolves.

With embeddings (Qwen3-Embedding-8B, 1024 dims), similarity is pgvector cosine over
`incidents.embedding` of resolved incidents in the same workspace. Without them (no
PRICE_EMBED_PER_M), a fallback compares the detector rules and affected resources, so a re-run of
the same scenario still finds its predecessor. Test incidents are never found. A hit at or above
MEDIC_MEMORY_THRESHOLD is reused: the past diagnosis and fix go into the prompt, research is
skipped, and the diagnosis runs on the cheaper `confirm_root_cause` route. An incident is found
only from `remembered_at`: at resolve, or, with a fix PR still open then, once the PR is merged
or closed (app.incidents.followups); a closed PR's fix is left out ("rollback only").
"""

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.medic.events import ChangeMarker, MemoryHit
from app.db.models import Incident, Resource
from app.llm.budget import RunBudget
from app.llm.client import LLMConfigError, LLMError, TokenFactory

MAX_HITS = 3
# The fallback's score for the same rules on the same resources.
FALLBACK_MAX = 0.95


async def symptom_text(session: AsyncSession, incident: Incident) -> str:
    names = list(
        await session.scalars(
            select(Resource.name).where(Resource.id.in_(incident.affected_resource_ids))
        )
    )
    rules = sorted({str(s.get("rule")) for s in incident.signals})
    details = "; ".join(str(s.get("detail")) for s in incident.signals[:5])
    resources = ", ".join(sorted(names))
    return f"{incident.title}. Resources: {resources}. Rules: {', '.join(rules)}. {details}"


async def embed_symptom(llm: TokenFactory, budget: RunBudget, text: str) -> list[float] | None:
    try:
        return (await llm.embed([text], budget=budget))[0]
    except (LLMConfigError, LLMError):
        return None


def _hit(past: Incident, similarity: float) -> MemoryHit:
    state: dict[str, Any] = past.state or {}
    root = state.get("root_cause") or {}
    applied, unmerged = [], False
    for f in state.get("fixes", []):
        if f.get("status") != "applied":
            continue
        if (f.get("pull_request") or {}).get("state") == "closed":
            unmerged = True  # opened, never merged: not part of what fixed it
            continue
        applied.append(str((f.get("fix") or {}).get("title")))
    summary = "; ".join(applied)
    if unmerged:
        summary = f"{summary} (rollback only: the fix PR was closed without merging)".strip()
    return MemoryHit(
        incident_id=past.id,
        title=past.title,
        similarity=round(similarity, 3),
        root_cause=root.get("summary") or past.root_cause,
        fix_summary=summary or None,
        resolved_at=past.resolved_at,
    )


def _jaccard(a: set[Any], b: set[Any]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def _contained(a: set[Any], b: set[Any]) -> float:
    """Overlap coefficient: 1.0 when one set holds the other."""
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


_SECRET = re.compile(r":secret:([^:]+?)(?:-[A-Za-z0-9]{6})?(?::|$)")


def change_target(kind: str | None, ref: str | None, label: str | None = None) -> str | None:
    """What a change was made to, the same across versions: "secret:checkout/database-url" for
    any version of that secret. Only for changes that are the same fault when repeated (a secret
    or config); a new commit or deploy is a different change every time."""
    if kind == "secret_change" and ref:
        found = _SECRET.search(ref)
        return f"secret:{found.group(1)}" if found else None
    if kind == "config" and (ref or label):
        return f"config:{ref or label}"
    return None


def _past_target(past: Incident) -> str | None:
    change = ((past.state or {}).get("root_cause") or {}).get("triggering_change") or {}
    return change_target(change.get("kind"), change.get("ref"), change.get("description"))


async def similar(
    session: AsyncSession,
    incident: Incident,
    vector: list[float] | None,
    changes: list[ChangeMarker] | None = None,
) -> list[MemoryHit]:
    base = select(Incident).where(
        Incident.workspace_id == incident.workspace_id,
        Incident.id != incident.id,
        Incident.status == "resolved",
        # Test incidents (POST /incidents with test: true) are never reused for real ones.
        Incident.is_test.is_(False),
        # Not while its fix PR is still open: that fix was not applied yet.
        Incident.remembered_at.is_not(None),
    )
    if vector is not None:
        distance = Incident.embedding.cosine_distance(vector)
        rows = (
            await session.execute(
                base.add_columns(distance)
                .where(Incident.embedding.is_not(None))
                .order_by(distance)
                .limit(MAX_HITS)
            )
        ).all()
        return [_hit(past, 1.0 - float(d)) for past, d in rows]
    rules = {s.get("rule") for s in incident.signals}
    affected = set(incident.affected_resource_ids)
    # Changes in this incident's window, by what they changed (any version of the secret).
    targets = {t for c in changes or [] if (t := change_target(c.kind, c.ref, c.label))}
    scored = []
    for past in await session.scalars(base.order_by(Incident.resolved_at.desc()).limit(50)):
        past_rules = {s.get("rule") for s in past.signals}
        past_resources = set(past.affected_resource_ids)
        if _past_target(past) in targets:
            # The same thing changed again before the same symptoms (checkout/database-url,
            # then connection failures): the root cause most likely repeats. One service of
            # the same outage (the worker's errors of an api + worker outage) is not penalized
            # for being part of it: containment, not Jaccard.
            score = FALLBACK_MAX * (
                0.25 * _contained(rules, past_rules)
                + 0.25 * _contained(affected, past_resources)
                + 0.5
            )
        else:
            score = FALLBACK_MAX * (
                0.5 * _jaccard(rules, past_rules) + 0.5 * _jaccard(affected, past_resources)
            )
        if score > 0:
            scored.append((score, past))
    scored.sort(key=lambda pair: -pair[0])
    return [_hit(past, score) for score, past in scored[:MAX_HITS]]


def reusable(hits: list[MemoryHit], threshold: float) -> MemoryHit | None:
    return hits[0] if hits and hits[0].similarity >= threshold else None


def remember(incident: Incident, summary: str, root_cause: str, vector: list[float] | None) -> None:
    """Store what future incidents compare against. The caller commits. A test incident keeps
    its summary but no embedding, so nothing can find it."""
    incident.summary = summary
    incident.root_cause = root_cause
    if incident.is_test:
        incident.embedding = None
    elif vector is not None:
        incident.embedding = vector
