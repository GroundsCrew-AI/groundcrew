"""Incident memory: similar resolved incidents, and remembering this one when it resolves.

With embeddings (Qwen3-Embedding-8B, 1024 dims), similarity is pgvector cosine over
`incidents.embedding` of resolved incidents in the same workspace. Without them (no
PRICE_EMBED_PER_M), a fallback compares the detector rules and affected resources, so a re-run of
the same scenario still finds its predecessor. Test incidents are never found. A hit at or above
MEDIC_MEMORY_THRESHOLD is reused: the past diagnosis and fix go into the prompt, research is
skipped, and the diagnosis runs on the cheaper `confirm_root_cause` route.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.medic.events import MemoryHit
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
    applied = [
        str((f.get("fix") or {}).get("title"))
        for f in state.get("fixes", [])
        if f.get("status") == "applied"
    ]
    return MemoryHit(
        incident_id=past.id,
        title=past.title,
        similarity=round(similarity, 3),
        root_cause=root.get("summary") or past.root_cause,
        fix_summary="; ".join(applied) or None,
        resolved_at=past.resolved_at,
    )


def _jaccard(a: set[Any], b: set[Any]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


async def similar(
    session: AsyncSession, incident: Incident, vector: list[float] | None
) -> list[MemoryHit]:
    base = select(Incident).where(
        Incident.workspace_id == incident.workspace_id,
        Incident.id != incident.id,
        Incident.status == "resolved",
        # Test incidents (POST /incidents with test: true) are never reused for real ones.
        Incident.is_test.is_(False),
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
    scored = []
    for past in await session.scalars(base.order_by(Incident.resolved_at.desc()).limit(50)):
        score = FALLBACK_MAX * (
            0.5 * _jaccard(rules, {s.get("rule") for s in past.signals})
            + 0.5 * _jaccard(affected, set(past.affected_resource_ids))
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
