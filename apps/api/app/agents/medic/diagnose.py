"""The diagnosis: one structured call, then a post-check that keeps only cited claims.

The model sees the incident, the ranked evidence items with their ids, the change markers, and
(as context, not evidence) research results and a reused past diagnosis. It must cite ids.
`check()` removes ids that are not in the evidence, drops claims left with none, and drops a
triggering change that neither cites evidence nor matches a collected change marker. A
diagnosis with no cited claim left is marked low confidence.
"""

import json
import re
from datetime import UTC, datetime, timedelta

from app.agents.medic.events import ChangeMarker, MemoryHit, ResearchResult, Signal
from app.agents.medic.schemas import EvidenceItem, RootCause
from app.llm.budget import RunBudget
from app.llm.client import OnCall, TokenFactory
from app.llm.prompting import UNTRUSTED_RULE, fence
from app.llm.router import Task

SYSTEM = (
    """You are Medic, an SRE investigating a production incident in a customer's cloud.
Work only from the evidence lines given. Each line has an id like ev-1a2b3c4d5e.

Rules:
- Every claim cites the ids of the evidence lines it rests on, in `evidence_ids`. Use only ids
  that appear in the evidence. A claim you cannot cite does not belong in the answer.
- `triggering_change` is the change that started the incident (a commit, a deploy, a secret
  change, a config change) with its ref and time, citing evidence. Use null if none is shown.
  If there is no commit or deploy near the start, say so rather than inventing one.
  A deploy acts from when it starts, not when its new tasks are live: a migration it runs
  changes the database first. A break that begins between a deploy's start and its completion
  points to that deploy and the commit it ships.
- `root_cause` explains what broke and why in two to four sentences. `summary` is one sentence.
- Running is not healthy: "1/1 tasks running" says nothing about health. Never call a service
  recovered or healthy from its task count; only passing health checks and an error rate back
  to normal show recovery, and if they are not in the evidence, the outage is ongoing.
- `fix_options` lists fixes in order: an emergency action when production is down now
  (ecs_rollback_service to the previous task definition, secret_rollback_version to the
  previous secret version, force_redeploy so tasks re-read config), and a pull_request that
  fixes the cause in the repository when the cause is in code, a task definition, or a migration.
- A metric spike that starts before the triggering change was not caused by it (a load test,
  a batch job): do not cite it for this incident.
- `false_alarm` is true only when nothing is wrong for users: the "errors" are not errors
  (INFO or DEBUG lines from a logger whose name contains "error", a normal restart) AND the
  evidence shows
  normal error rates, no HTTP 5xx, no timeouts, and normal latency and database CPU. HTTP 5xx,
  statement timeouts, slow requests, or high database CPU are a real outage, even if some of
  the alert's lines were noise: find its cause. A false alarm has no fix_options.
- `search_query` is a short web search for the root cause's mechanism in this stack's terms,
  with no names, ids, or numbers (for example the database engine and the failure mode).
- Research results and past incidents are background, not evidence: never cite them.
- Use "other" or null when nothing fits. Never ask for or reveal secret values.
- """
    + UNTRUSTED_RULE
)

_SHA = re.compile(r"^[0-9a-f]{7,40}$")
# Metric points are per minute and stamped at the minute's start.
SPIKE_BUCKET = timedelta(minutes=1)


def _time(at: str | None) -> datetime | None:
    if not at:
        return None
    try:
        parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def matched_change(root_cause: RootCause, changes: list[ChangeMarker]) -> ChangeMarker | None:
    """The collected change marker the triggering change refers to, if any."""
    change = root_cause.triggering_change
    if change is None or not change.ref:
        return None
    return next((c for c in changes if _same_change(change.ref, c.ref)), None)


def change_at(root_cause: RootCause, changes: list[ChangeMarker]) -> datetime | None:
    """When the triggering change happened: the matched marker's time, never the model's own
    `at` (a wrong time would drop evidence and move the incident's start)."""
    marker = matched_change(root_cause, changes)
    return marker.at if marker is not None else None


def _before_change(item: EvidenceItem, at: datetime | None) -> bool:
    """A spike whose whole minute ended before the triggering change."""
    stamp = _time(item.timestamp)
    return (
        at is not None
        and stamp is not None
        and "[SPIKE]" in item.line
        and (stamp + SPIKE_BUCKET <= at)
    )


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
        + fence(
            "changes",
            "\n".join(f"- {c.at.isoformat()} {c.kind} {c.ref}: {c.label}" for c in changes)
            or "- none",
        ),
        "Evidence:\n" + fence("evidence", _evidence_block(items)),
    ]
    if research:
        parts.append(
            "Background from web research (not evidence):\n"
            + fence(
                "research",
                "\n".join(f"- {r.title} ({r.url}): {r.snippet[:300]}" for r in research),
            )
        )
    if memory is not None:
        parts.append(
            "A similar past incident (not evidence; confirm or reject it from this incident's "
            "evidence):\n"
            + fence(
                "past_incident",
                f"{memory.title}. Root cause then: {memory.root_cause}. "
                f"Fix then: {memory.fix_summary}.",
            )
        )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n\n".join(parts)}]


def _same_change(ref: str, marker: str) -> bool:
    """A model's ref names a collected change: the same ref, a commit SHA prefix (7+ hex), or
    a long prefix of the marker (an ARN without its version suffix). Short or generic refs
    ("main", "1") never match."""
    ref, marker = ref.strip(), marker.strip()
    if not ref or not marker:
        return False
    if ref == marker:
        return True
    if _SHA.match(ref):
        return marker.startswith(ref)
    return len(ref) >= 20 and marker.startswith(ref)


def _matches_change(ref: str | None, changes: list[ChangeMarker]) -> bool:
    return bool(ref) and any(_same_change(ref or "", c.ref) for c in changes)


def check(
    root_cause: RootCause, items: list[EvidenceItem], changes: list[ChangeMarker]
) -> tuple[RootCause, list[str]]:
    valid = {i.id for i in items}
    dropped: list[str] = []
    claims = []
    at = change_at(root_cause, changes)
    earlier = {i.id for i in items if _before_change(i, at)}
    for claim in root_cause.claims:
        ids = [i for i in claim.evidence_ids if i in valid and i not in earlier]
        unknown = [i for i in claim.evidence_ids if i not in valid]
        if unknown:
            dropped.append(f"unknown evidence ids removed: {', '.join(unknown[:5])}")
        before = [i for i in claim.evidence_ids if i in earlier]
        if before:
            dropped.append(f"spikes before the triggering change removed: {', '.join(before[:5])}")
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


def not_false_alarm(failing: list[str]) -> str:
    """The follow-up when live checks contradict a "false alarm" diagnosis."""
    return (
        "Medic checked the affected resources now, and they are not normal:\n"
        + "\n".join(f"- {f}" for f in failing)
        + "\nSo this is not a false alarm. Diagnose the real outage from the evidence (HTTP "
        "5xx, timeouts, slow requests, database load) and set false_alarm to false."
    )


def summary_json(root_cause: RootCause) -> str:
    return json.dumps(root_cause.model_dump(mode="json"))[:4000]
