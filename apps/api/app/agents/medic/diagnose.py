"""The diagnosis: one structured call, then a post-check that keeps only cited claims.

The model sees the incident, the ranked evidence items with their ids, the change markers, and
(as context, not evidence) research results and a reused past diagnosis. It must cite ids.
`check()` removes ids that are not in the evidence, drops claims left with none, and drops a
triggering change that neither cites evidence nor matches a collected change marker. A
diagnosis with no cited claim left is marked low confidence.
"""

import json
import re

from app.agents.medic.events import ChangeMarker, MemoryHit, ResearchResult, Signal
from app.agents.medic.schemas import EvidenceItem, RootCause
from app.llm.budget import RunBudget
from app.llm.client import OnCall, TokenFactory
from app.llm.router import Task

SYSTEM = """You are Medic, an SRE investigating a production incident in a customer's cloud.
Work only from the evidence lines given. Each line has an id like ev-1a2b3c4d5e.

Rules:
- Every claim cites the ids of the evidence lines it rests on, in `evidence_ids`. Use only ids
  that appear in the evidence. A claim you cannot cite does not belong in the answer.
- `triggering_change` is the change that started the incident (a commit, a deploy, a secret
  change, a config change) with its ref and time, citing evidence. Use null if none is shown.
  If there is no commit or deploy near the start, say so rather than inventing one.
- `root_cause` explains what broke and why in two to four sentences. `summary` is one sentence.
- `fix_options` lists fixes in order: an emergency action when production is down now
  (ecs_rollback_service to the previous task definition, secret_rollback_version to the
  previous secret version, force_redeploy so tasks re-read config), and a pull_request that
  fixes the cause in the repository when the cause is in code, a task definition, or a migration.
- Research results and past incidents are background, not evidence: never cite them.
- Use "other" or null when nothing fits. Never ask for or reveal secret values."""

_SHA = re.compile(r"^[0-9a-f]{7,40}$")


def _evidence_block(items: list[EvidenceItem]) -> str:
    return "\n".join(f"[{i.id}] {i.timestamp} | {i.source} | {i.line}" for i in items)


def messages(
    title: str,
    signals: list[Signal],
    affected: list[str],
    items: list[EvidenceItem],
    changes: list[ChangeMarker],
    research: list[ResearchResult],
    memory: MemoryHit | None,
) -> list[dict[str, str]]:
    parts = [
        f"Incident: {title}",
        "Signals:\n"
        + "\n".join(
            f"- {s.at.isoformat()} {s.rule} on {s.resource_name}: {s.detail}" for s in signals
        ),
        f"Affected resources: {', '.join(affected)}",
        "Changes seen in the window:\n"
        + (
            "\n".join(f"- {c.at.isoformat()} {c.kind} {c.ref}: {c.label}" for c in changes)
            or "- none"
        ),
        "Evidence:\n" + _evidence_block(items),
    ]
    if research:
        parts.append(
            "Background from web research (not evidence):\n"
            + "\n".join(f"- {r.title} ({r.url}): {r.snippet[:300]}" for r in research)
        )
    if memory is not None:
        parts.append(
            "A similar past incident (not evidence; confirm or reject it from this incident's "
            f"evidence): {memory.title}. Root cause then: {memory.root_cause}. "
            f"Fix then: {memory.fix_summary}."
        )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n\n".join(parts)}]


def _matches_change(ref: str | None, changes: list[ChangeMarker]) -> bool:
    if not ref:
        return False
    for c in changes:
        if ref == c.ref or (c.ref and (ref in c.ref or c.ref in ref)):
            return True
        if _SHA.match(ref) and c.ref.startswith(ref):
            return True
    return False


def check(
    root_cause: RootCause, items: list[EvidenceItem], changes: list[ChangeMarker]
) -> tuple[RootCause, list[str]]:
    valid = {i.id for i in items}
    dropped: list[str] = []
    claims = []
    for claim in root_cause.claims:
        ids = [i for i in claim.evidence_ids if i in valid]
        unknown = [i for i in claim.evidence_ids if i not in valid]
        if unknown:
            dropped.append(f"unknown evidence ids removed: {', '.join(unknown[:5])}")
        if ids:
            claims.append(claim.model_copy(update={"evidence_ids": ids}))
        else:
            dropped.append(f"uncited claim: {claim.text[:200]}")
    change = root_cause.triggering_change
    if change is not None:
        ids = [i for i in change.evidence_ids if i in valid]
        if ids or _matches_change(change.ref, changes):
            change = change.model_copy(update={"evidence_ids": ids})
        else:
            dropped.append(f"uncited triggering change: {change.kind} {change.ref}")
            change = None
    confidence = root_cause.confidence if claims else "low"
    checked = root_cause.model_copy(
        update={"claims": claims, "triggering_change": change, "confidence": confidence}
    )
    return checked, dropped


async def diagnose(
    llm: TokenFactory,
    budget: RunBudget,
    prompt: list[dict[str, str]],
    *,
    confirm: bool,
    on_call: OnCall | None = None,
) -> RootCause:
    task = Task.confirm_root_cause if confirm else Task.root_cause
    result = await llm.chat(task, prompt, budget=budget, schema=RootCause, on_call=on_call)
    parsed: RootCause = result.parsed
    return parsed


def summary_json(root_cause: RootCause) -> str:
    return json.dumps(root_cause.model_dump(mode="json"))[:4000]
