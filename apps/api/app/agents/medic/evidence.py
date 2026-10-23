"""From collected evidence to the ranked, citable items the diagnosis sees.

Every line becomes an `EvidenceItem` with a stable id. Ranking is two passes:

1. A heuristic score: OOM kills and exit codes, marked spikes and slow minutes, errors and
   failed health checks, changes (commits, deploys, secret changes), and lines about the
   affected resources score high; every source keeps at least its two best lines, so no reader
   is drowned out.
2. When embeddings are configured (MODEL_EMBED and PRICE_EMBED_PER_M), one embed call for the
   symptom and the candidates adds cosine similarity to the score (Token Factory has no rerank
   model; PROJECT_CONTEXT.md decision of 2026-10-07). Without them the heuristic stands.

The diagnosis gets the top MAX_ITEMS, oldest first.
"""

import re
import uuid
from collections import defaultdict

from app.agents.medic.schemas import EvidenceItem, evidence_id
from app.llm.budget import RunBudget
from app.llm.client import LLMConfigError, LLMError, TokenFactory
from app.llm.vectors import cosine
from app.telemetry.collect import Evidence

MAX_ITEMS = 40
MAX_CANDIDATES = 120
PER_SOURCE_FLOOR = 2
COSINE_WEIGHT = 4.0

_WEIGHTS: tuple[tuple[re.Pattern[str], float], ...] = (
    (re.compile(r"\[OOM\]|OutOfMemory|exit code 137"), 5.0),
    (re.compile(r"\[SPIKE\]"), 4.0),
    (re.compile(r"inside the incident window"), 4.0),
    (re.compile(r"(?i)\b(unreachable|refused|traceback|exception|fatal|panic)\b"), 3.5),
    (re.compile(r"(?i)health ?checks?|rollout FAILED|deployment failed|circuit breaker"), 3.5),
    (re.compile(r"\bUNHEALTHY\b|\bunhealthy\b|targets healthy"), 3.5),
    # What a deploy changed (app.telemetry.ecs.compare): weighs on whether it is the cause.
    (re.compile(r"^task definition \S+ (vs |was registered at )"), 3.0),
    (re.compile(r"(?i)\b(error|failed|failure|timed? ?out|killed)\b"), 2.5),
    (re.compile(r"^commit |^workflow run |^production deployment |^deployment "), 2.5),
    (re.compile(r"\[SLOW\]"), 3.0),
    # Any path but a health check (its latency jumps without a problem).
    (
        re.compile(r"^latency summary path=(?!/(health|ready|live|ping))\S* .*p50=[1-9]\d{2,}ms"),
        2.0,
    ),
    (re.compile(r"^now: "), 1.0),
    (re.compile(r"no commits or workflow runs"), 1.5),
)


def to_items(evidence: Evidence) -> list[EvidenceItem]:
    items: dict[str, EvidenceItem] = {}
    for key, lines in evidence.sources.items():
        resource_id = evidence.source_resources.get(key)
        for line in lines:
            stamp = line.at.isoformat()
            item_id = evidence_id(key, stamp, line.message)
            items[item_id] = EvidenceItem(
                id=item_id, source=key, timestamp=stamp, resource_id=resource_id, line=line.message
            )
    return list(items.values())


def score(item: EvidenceItem, affected: set[uuid.UUID]) -> float:
    total = sum(weight for pattern, weight in _WEIGHTS if pattern.search(item.line))
    if item.resource_id in affected:
        total += 1.0
    if "reader failed" in item.line or "reader timed out" in item.line:
        total -= 2.0
    return total


def heuristic_pick(
    items: list[EvidenceItem], affected: set[uuid.UUID], limit: int
) -> list[tuple[EvidenceItem, float]]:
    scored = sorted(
        ((item, score(item, affected)) for item in items),
        key=lambda pair: (-pair[1], pair[0].timestamp),
    )
    by_source: dict[str, list[tuple[EvidenceItem, float]]] = defaultdict(list)
    for pair in scored:
        by_source[pair[0].source].append(pair)
    chosen = {p[0].id: p for pairs in by_source.values() for p in pairs[:PER_SOURCE_FLOOR]}
    for pair in scored:
        if len(chosen) >= limit:
            break
        chosen.setdefault(pair[0].id, pair)
    return sorted(chosen.values(), key=lambda pair: -pair[1])


async def rank(
    items: list[EvidenceItem],
    symptom: str,
    affected: set[uuid.UUID],
    llm: TokenFactory,
    budget: RunBudget,
    limit: int = MAX_ITEMS,
) -> tuple[list[EvidenceItem], bool]:
    """The items the diagnosis sees, oldest first, and whether embeddings were used."""
    candidates = heuristic_pick(items, affected, MAX_CANDIDATES)
    used = False
    if candidates:
        try:
            query, *vectors = await llm.embed(
                [symptom, *(f"{c.source}: {c.line}" for c, _ in candidates)], budget=budget
            )
        except (LLMConfigError, LLMError):
            pass
        else:
            used = True
            rescored = [
                (c, s + COSINE_WEIGHT * cosine(query, v))
                for (c, s), v in zip(candidates, vectors, strict=True)
            ]
            candidates = sorted(rescored, key=lambda pair: -pair[1])
    top = heuristic_floor(candidates, limit)
    return sorted(top, key=lambda item: (item.timestamp, item.id)), used


def heuristic_floor(candidates: list[tuple[EvidenceItem, float]], limit: int) -> list[EvidenceItem]:
    """The best `limit`, keeping each source's best line."""
    best: dict[str, EvidenceItem] = {}
    for item, _ in candidates:
        best.setdefault(item.source, item)
    chosen = {item.id: item for item in best.values()}
    for item, _ in candidates:
        if len(chosen) >= limit:
            break
        chosen.setdefault(item.id, item)
    return list(chosen.values())
