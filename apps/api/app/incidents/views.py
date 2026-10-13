"""Incident rows and Medic's state as the API's IncidentSummary / IncidentDetail
(docs/phase5-interfaces.md, section 6). Totals are summed over all of an incident's runs.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, get_args

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.medic.events import (
    DetectedBy,
    DetectorRule,
    IncidentStatus,
    ResourceRef,
    Severity,
    Signal,
    TimeWindow,
)
from app.agents.medic.state import MedicState
from app.connectors.github import GitHubApp, GitHubError, check_repo_name
from app.db.models import Incident, Resource, Run
from app.routes.incident_schemas import (
    FixState,
    IncidentDetail,
    IncidentOutcome,
    IncidentSummary,
    MemoryUse,
)
from app.telemetry.evidence import parse_time

PR_REFRESH_EVERY = timedelta(seconds=60)


@dataclass
class Totals:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Decimal | None = None
    ultra_equiv_usd: Decimal | None = None
    latest_run: uuid.UUID | None = None
    latest_at: datetime | None = None
    run_ids: list[uuid.UUID] | None = None


def _pick[T](value: Any, allowed: Any, default: T) -> T:
    return value if value in get_args(allowed) else default


async def _totals(session: AsyncSession, incident_ids: list[uuid.UUID]) -> dict[uuid.UUID, Totals]:
    out: dict[uuid.UUID, Totals] = defaultdict(Totals)
    if not incident_ids:
        return out
    runs = await session.scalars(
        select(Run).where(Run.incident_id.in_(incident_ids)).order_by(Run.created_at, Run.id)
    )
    for run in runs:
        if run.incident_id is None:
            continue
        t = out[run.incident_id]
        t.prompt_tokens += run.prompt_tokens
        t.completion_tokens += run.completion_tokens
        if run.cost_usd is not None:
            t.cost_usd = (t.cost_usd or Decimal(0)) + run.cost_usd
        if run.ultra_equiv_usd is not None:
            t.ultra_equiv_usd = (t.ultra_equiv_usd or Decimal(0)) + run.ultra_equiv_usd
        t.latest_run, t.latest_at = run.id, run.created_at
        t.run_ids = [*(t.run_ids or []), run.id]
    return out


async def _refs(session: AsyncSession, incidents: list[Incident]) -> dict[uuid.UUID, ResourceRef]:
    ids = {rid for i in incidents for rid in i.affected_resource_ids}
    if not ids:
        return {}
    rows = await session.scalars(select(Resource).where(Resource.id.in_(ids)))
    return {r.id: ResourceRef(id=r.id, name=r.name, type=r.type, provider=r.provider) for r in rows}


def _ms(later: datetime | None, earlier: datetime | None) -> int | None:
    if later is None or earlier is None:
        return None
    return max(int((later - earlier).total_seconds() * 1000), 0)


def _summary_fields(
    incident: Incident, state: MedicState, refs: dict[uuid.UUID, ResourceRef], t: Totals
) -> dict[str, Any]:
    applied = [r for r in state.fixes if r.status == "applied"]
    rc = state.root_cause
    return {
        "id": incident.id,
        "workspace_id": incident.workspace_id,
        "run_id": t.latest_run,
        "title": incident.title,
        "status": _pick(incident.status, IncidentStatus, "open"),
        "severity": _pick(incident.severity, Severity, "medium"),
        "detected_by": _pick(incident.detected_by, DetectedBy, "user"),
        "test": incident.is_test,
        "detector_rule": _pick(incident.detector_rule, DetectorRule, "other"),
        "opened_at": incident.created_at,
        "resolved_at": incident.resolved_at,
        "window": TimeWindow(start=incident.window_start, end=incident.window_end),
        "affected_resource_ids": list(incident.affected_resource_ids),
        "affected_resources": [refs[r] for r in incident.affected_resource_ids if r in refs],
        "root_cause_summary": rc.summary if rc else None,
        "triggering_change": rc.triggering_change if rc else None,
        "fixes_applied": [r.fix.action or "pull_request" for r in applied],
        "pull_requests": [r.pull_request for r in state.fixes if r.pull_request],
        "memory_reused": state.memory is not None,
        "prompt_tokens": t.prompt_tokens,
        "completion_tokens": t.completion_tokens,
        "total_tokens": t.prompt_tokens + t.completion_tokens,
        "cost_usd": float(t.cost_usd) if t.cost_usd is not None else None,
        "ultra_equiv_usd": float(t.ultra_equiv_usd) if t.ultra_equiv_usd is not None else None,
        "time_to_detect_ms": _ms(incident.created_at, incident.first_signal_at),
        "duration_ms": _ms(incident.resolved_at, incident.created_at),
    }


async def summaries(session: AsyncSession, incidents: list[Incident]) -> list[IncidentSummary]:
    totals = await _totals(session, [i.id for i in incidents])
    refs = await _refs(session, incidents)
    return [
        IncidentSummary(
            **_summary_fields(i, MedicState.model_validate(i.state or {}), refs, totals[i.id])
        )
        for i in incidents
    ]


async def detail(session: AsyncSession, incident: Incident) -> IncidentDetail:
    state = MedicState.model_validate(incident.state or {})
    totals = (await _totals(session, [incident.id]))[incident.id]
    refs = await _refs(session, [incident])
    outcome = (
        IncidentOutcome(
            result=state.outcome.result,
            reason=state.outcome.reason,
            still_failing=state.outcome.still_failing,
        )
        if state.outcome
        else None
    )
    memory = (
        MemoryUse(
            incident_id=state.memory.incident_id,
            title=state.memory.title,
            similarity=state.memory.similarity,
            reused=state.memory.reused,
        )
        if state.memory
        else None
    )
    return IncidentDetail(
        **_summary_fields(incident, state, refs, totals),
        signals=[Signal.model_validate(s) for s in incident.signals],
        evidence=state.evidence,
        changes=state.changes,
        root_cause=state.root_cause,
        fixes=[
            FixState(
                fix=r.fix,
                status=r.status,
                approval_id=r.approval_id,
                pull_request=r.pull_request,
                applied=r.applied,
            )
            for r in state.fixes
        ],
        verify=state.verify,
        outcome=outcome,
        memory=memory,
        research=state.research,
        run_ids=totals.run_ids or [],
        error=state.error,
    )


async def refresh_pull_requests(
    incident: Incident, app: GitHubApp, *, now: datetime | None = None
) -> bool:
    """Update the state of open fix PRs from GitHub (GET only), at most once a minute per
    incident. True when something changed; the caller commits. Errors leave the old state."""
    now = now or datetime.now(UTC)
    state = MedicState.model_validate(incident.state or {})
    checked = state.model_extra.get("prs_checked_at") if state.model_extra else None
    if checked and now - datetime.fromisoformat(str(checked)) < PR_REFRESH_EVERY:
        return False
    open_prs = [r for r in state.fixes if r.pull_request and r.pull_request.state == "open"]
    if not open_prs or not state.fixes:
        return False
    changed = False
    for rec in open_prs:
        pr, draft = rec.pull_request, rec.draft
        if pr is None or draft is None:
            continue
        try:
            data = await app.get(
                draft.installation_id, f"/repos/{check_repo_name(pr.repo)}/pulls/{pr.number}"
            )
        except GitHubError:
            continue
        new = (
            "merged"
            if data.get("merged")
            else ("closed" if data.get("state") == "closed" else "open")
        )
        if new != pr.state:
            merged_at = parse_time(data.get("merged_at")) if new == "merged" else None
            rec.pull_request = pr.model_copy(update={"state": new, "merged_at": merged_at})
            changed = True
    dumped = state.model_dump(mode="json")
    dumped["prs_checked_at"] = now.isoformat()
    incident.state = dumped
    return changed
