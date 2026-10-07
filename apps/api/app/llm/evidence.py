"""Evidence funnel for Medic (Phase 5). Not wired into anything yet.

raw logs / metrics / events (100k+ tokens)
  -> chunk to fit Nano's context
  -> Nano compresses each chunk (in parallel) into structured findings
  -> rank findings against the symptom (embedding cosine similarity; a rerank model if one is
     configured; severity if embeddings are unavailable)
  -> Ultra reasons over about 8 findings (a few thousand tokens)

This is what keeps "Ultra for root cause" affordable, and it shows the three tiers working.

TODO(budget): distill() runs up to `concurrency` calls at once against one RunBudget. Each
call checks the budget before any of them has recorded its spend, so a run can overshoot its
caps by up to `concurrency` Nano calls. Reserve spend per call before wiring this into Medic.
"""

import asyncio
from typing import Literal

from pydantic import BaseModel, Field

from app.llm.budget import RunBudget
from app.llm.client import LLMConfigError, TokenFactory
from app.llm.models import Tier
from app.llm.router import Task
from app.llm.vectors import cosine

CHARS_PER_TOKEN = 4  # rough; good enough for chunk sizing


class Finding(BaseModel):
    source: str = Field(description="e.g. cloudwatch:/ecs/worker, azure-monitor:api")
    time_range: str
    kind: Literal["error", "latency", "resource", "deploy", "config", "other"]
    summary: str = Field(description="one sentence, concrete, includes the key values")
    quote: str = Field(description="the single most telling raw line, verbatim, <200 chars")
    severity: int = Field(ge=0, le=3)


class Findings(BaseModel):
    findings: list[Finding]


def chunk(text: str, max_tokens: int) -> list[str]:
    """Split on line boundaries into pieces of about `max_tokens` tokens."""
    size = max_tokens * CHARS_PER_TOKEN
    out: list[str] = []
    current: list[str] = []
    length = 0
    for line in text.splitlines():
        if length + len(line) > size and current:
            out.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        out.append("\n".join(current))
    return out


async def distill(
    tf: TokenFactory,
    budget: RunBudget,
    symptom: str,
    sources: dict[str, str],
    top_k: int = 8,
    concurrency: int = 6,
) -> list[Finding]:
    # Leave room for the prompt and the output inside Nano's usable window: about 5k tokens of
    # evidence per chunk with LLM_CONTEXT_NANO=8000 (Nano reads reliably only up to about 6k).
    max_chunk = int(getattr(tf.settings, f"llm_context_{Tier.nano}") * 0.6)
    semaphore = asyncio.Semaphore(concurrency)

    async def one(source: str, text: str) -> list[Finding]:
        async with semaphore:
            result = await tf.chat(
                Task.compress_evidence,
                [
                    {
                        "role": "user",
                        "content": f"Incident symptom: {symptom}\nSource: {source}\n"
                        "Extract only findings that could explain or relate to the symptom. "
                        "Return an empty list if nothing is relevant.\n\n" + text,
                    }
                ],
                budget=budget,
                schema=Findings,
            )
            parsed: Findings = result.parsed
            return parsed.findings

    jobs = [one(src, part) for src, text in sources.items() for part in chunk(text, max_chunk)]
    findings = [f for batch in await asyncio.gather(*jobs) for f in batch]
    if not findings:
        return []

    return await rank(tf, budget, symptom, findings, top_k)


def _finding_text(finding: Finding) -> str:
    return f"{finding.kind}: {finding.summary} | {finding.quote}"


async def rank(
    tf: TokenFactory, budget: RunBudget, symptom: str, findings: list[Finding], top_k: int = 8
) -> list[Finding]:
    """The `top_k` findings most related to the symptom.

    Token Factory has no rerank model (probe, 2026-10-07), so the default is cosine similarity
    of embeddings (one embed call for the symptom and all findings). A rerank model is used
    only when MODEL_RERANK is set. Without embeddings (no model or price), most severe first.
    """
    if not findings:
        return []
    texts = [_finding_text(f) for f in findings]
    if tf.settings.model_rerank:
        ranked = await tf.rerank(symptom, texts, top_k, budget=budget)
        return [findings[i] for i, _ in ranked]
    try:
        query, *vectors = await tf.embed([symptom, *texts], budget=budget)
    except LLMConfigError:
        return sorted(findings, key=lambda f: -f.severity)[:top_k]
    scored = sorted(
        range(len(findings)),
        key=lambda i: (-cosine(query, vectors[i]), -findings[i].severity, i),
    )
    return [findings[i] for i in scored[:top_k]]
