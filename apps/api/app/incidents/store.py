"""Incident rows: open (with dedupe), status changes, and the Medic run that investigates.

One open incident per resource group. An incident's group is its affected resources plus their
graph neighbors, one hop in either direction (the api's secret, log group, database, and its
callers). A report or watcher signal whose group overlaps an open incident's group joins that
incident (its signals are appended) instead of opening a second one: the api and the worker
share the database, so their problems are one incident whichever is reported first. Opening takes a
per-workspace advisory lock, so two watchers or a watcher and a person cannot race.

Every incident has a run (`agent = "medic"`, `runs.incident_id`). Its first step is
`incident_opened`; each status change is an `incident_updated` step, so the UI follows an
incident on the run stream (docs/phase5-interfaces.md, section 7).
"""

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, get_args

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import queue
from app.agents.medic.events import (
    DetectedBy,
    DetectorRule,
    IncidentOpened,
    IncidentStatus,
    IncidentUpdated,
    ResourceRef,
    Severity,
    Signal,
    TimeWindow,
)
from app.config import get_settings
from app.db.models import Edge, Incident, Resource, Run, RunStatus, StopReason
from app.steps import record_step

MEDIC_AGENT: Final = "medic"
INVESTIGATE_JOB: Final = "investigate_incident"
REDIAGNOSE_JOB: Final = "rediagnose_incident"
# An error spike this many times bigger than any before it is new evidence, not a repeat.
SPIKE_GROWTH = 3.0
_ERROR_LINES = re.compile(r"^(\d+) error lines")
OPEN_STATUSES: frozenset[str] = frozenset(
    s for s in get_args(IncidentStatus) if s not in ("resolved", "failed", "merged")
)
MAX_SIGNALS = 50
# Signals before the first one still belong to the incident's story (a deploy, a secret change).
LEAD_TIME = timedelta(minutes=30)

# Severity from the rule that fired, when the report does not say.
_SEVERITY: dict[str, Severity] = {
    "health_check": "critical",
    "ecs_deployment_failed": "high",
    "ecs_tasks_stopped": "high",
    "log_error_spike": "medium",
    "rds_cpu_latency": "medium",
    "k8s_rollout_failed": "high",
    "k8s_pods_crashing": "high",
    "user_report": "high",
    "other": "medium",
}
_RANK: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class ResourceNotFoundError(LookupError):
    """A resource id is not in the workspace's graph."""


@dataclass(frozen=True)
class Opened:
    incident: Incident
    run: Run
    deduped: bool


async def resource_group(
    session: AsyncSession, workspace_id: uuid.UUID, resource_ids: list[uuid.UUID]
) -> list[uuid.UUID]:
    """The resources plus their one-hop graph neighbors, either direction."""
    rows = await session.execute(
        select(Edge.from_id, Edge.to_id).where(
            Edge.workspace_id == workspace_id,
            or_(Edge.from_id.in_(resource_ids), Edge.to_id.in_(resource_ids)),
        )
    )
    group = set(resource_ids)
    for a, b in rows:
        group.update((a, b))
    return sorted(group)


async def _resources(
    session: AsyncSession, workspace_id: uuid.UUID, resource_ids: list[uuid.UUID]
) -> list[Resource]:
    found = list(
        await session.scalars(
            select(Resource).where(
                Resource.workspace_id == workspace_id, Resource.id.in_(resource_ids)
            )
        )
    )
    missing = set(resource_ids) - {r.id for r in found}
    if missing:
        raise ResourceNotFoundError(f"{len(missing)} resources are not in this workspace.")
    return sorted(found, key=lambda r: resource_ids.index(r.id))


def _ref(r: Resource) -> ResourceRef:
    return ResourceRef(id=r.id, name=r.name, type=r.type, provider=r.provider)


def _title(signals: list[Signal], resources: list[Resource]) -> str:
    if signals and signals[0].rule != "user_report":
        first = signals[0]
        return f"{first.resource_name or resources[0].name}: {first.detail}"[:300]
    names = ", ".join(r.name for r in resources[:3])
    return f"Reported problem on {names}"[:300]


async def _merged_resources(
    session: AsyncSession, ids: list[uuid.UUID], known: list[Resource]
) -> list[Resource]:
    """The resources merged in from failed incidents (not already in `known`), in `ids` order."""
    have = {r.id for r in known}
    wanted = [i for i in ids if i not in have]
    if not wanted:
        return []
    found = {
        r.id: r for r in await session.scalars(select(Resource).where(Resource.id.in_(wanted)))
    }
    return [found[i] for i in wanted if i in found]


async def latest_run(session: AsyncSession, incident_id: uuid.UUID) -> Run | None:
    return await session.scalar(
        select(Run)
        .where(Run.incident_id == incident_id)
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(1)
    )


async def find_open(
    session: AsyncSession, workspace_id: uuid.UUID, group: list[uuid.UUID], is_test: bool = False
) -> Incident | None:
    """The oldest open incident whose group overlaps `group` (a report's own group, so the
    answer does not depend on which of two related reports came first)."""
    return await session.scalar(
        select(Incident)
        .where(
            Incident.workspace_id == workspace_id,
            Incident.status.in_(OPEN_STATUSES),
            Incident.group_resource_ids.overlap(group),
            Incident.is_test.is_(is_test),
        )
        .order_by(Incident.created_at)
        .limit(1)
    )


async def find_recent_failed(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    group: list[uuid.UUID],
    now: datetime,
    is_test: bool = False,
) -> list[Incident]:
    """Incidents on related resources that failed in the last LEAD_TIME, not stopped by a
    person: an outage Medic could not fix from one symptom (a worker's error spike) that shows
    up again on a neighbor (the api's health checks) is the same outage."""
    found = list(
        await session.scalars(
            select(Incident)
            .where(
                Incident.workspace_id == workspace_id,
                Incident.status == "failed",
                Incident.window_end >= now - LEAD_TIME,
                Incident.group_resource_ids.overlap(group),
                Incident.is_test.is_(is_test),
            )
            .order_by(Incident.created_at)
            .with_for_update()
        )
    )
    out = []
    for incident in found:
        run = await latest_run(session, incident.id)
        if run is None or run.stop_reason != StopReason.stopped_by_user:
            out.append(incident)
    return out


def _signal_key(signal: dict[str, object]) -> tuple[object, ...]:
    return (signal.get("rule"), signal.get("resource_id"), signal.get("detail"), signal.get("at"))


def _error_lines(signal: dict[str, object]) -> int:
    found = _ERROR_LINES.match(str(signal.get("detail") or ""))
    return int(found.group(1)) if found else 0


def material_signals(
    existing: list[dict[str, object]], fresh: list[dict[str, object]]
) -> list[dict[str, object]]:
    """New signals that change the picture: a rule the incident has not seen (slow queries,
    health checks after an error spike), a resource it has not seen (the api's errors after the
    worker's: the same outage on a neighbor), a spike that now means "down", or an error spike
    SPIKE_GROWTH times the largest before it (8 error lines, then 112)."""
    rules = {s.get("rule") for s in existing}
    resources = {s.get("resource_id") for s in existing}
    down = {s.get("resource_id") for s in existing if s.get("down")}
    largest = max((_error_lines(s) for s in existing), default=0)
    out = []
    for signal in fresh:
        if (
            signal.get("rule") not in rules
            or signal.get("resource_id") not in resources
            or (signal.get("down") and signal.get("resource_id") not in down)
            or (
                signal.get("rule") == "log_error_spike"
                and _error_lines(signal) >= max(SPIKE_GROWTH * largest, largest + 1)
            )
        ):
            out.append(signal)
        rules.add(signal.get("rule"))
        resources.add(signal.get("resource_id"))
        if signal.get("down"):
            down.add(signal.get("resource_id"))
        largest = max(largest, _error_lines(signal))
    return out


async def open_incident(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    resource_ids: list[uuid.UUID],
    detected_by: DetectedBy,
    signals: list[Signal],
    title: str | None = None,
    description: str | None = None,
    severity: Severity | None = None,
    since: datetime | None = None,
    now: datetime | None = None,
    is_test: bool = False,
) -> Opened:
    """Open an incident and its Medic run, or join the open incident that covers these
    resources. Enqueues the investigation. Commits."""
    now = now or datetime.now(UTC)
    unique_ids = list(dict.fromkeys(resource_ids))
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"incidents:{workspace_id}"}
    )
    resources = await _resources(session, workspace_id, unique_ids)
    if not signals:
        signals = [
            Signal(
                rule="user_report",
                resource_id=resources[0].id,
                resource_name=resources[0].name,
                detail=(description or "Reported by a person")[:500],
                at=now,
            )
        ]
    group = await resource_group(session, workspace_id, unique_ids)
    existing = await find_open(session, workspace_id, group, is_test)
    if existing is not None:
        known = {_signal_key(s) for s in existing.signals}
        fresh = [d for s in signals if _signal_key(d := s.model_dump(mode="json")) not in known]
        material = material_signals(existing.signals, fresh)
        existing.signals = [*existing.signals, *fresh][-MAX_SIGNALS:]
        if material and existing.status in ("investigating", "waiting_approval"):
            # The diagnosis (or the fixes waiting for approval) predates this evidence.
            run = await latest_run(session, existing.id)
            if run is not None:
                await queue.enqueue(
                    session,
                    REDIAGNOSE_JOB,
                    {
                        "incident_id": str(existing.id),
                        "run_id": str(run.id),
                        "signals": [f"{s['rule']}: {s['detail']}" for s in material],
                    },
                    max_attempts=1,
                )
        added = [i for i in unique_ids if i not in existing.affected_resource_ids]
        if added:
            existing.affected_resource_ids = [*existing.affected_resource_ids, *added]
            existing.group_resource_ids = sorted(
                set(existing.group_resource_ids)
                | set(await resource_group(session, workspace_id, added))
            )
        run = await latest_run(session, existing.id)
        await session.commit()
        if run is None:  # pragma: no cover - every incident is opened with a run
            raise RuntimeError(f"Incident {existing.id} has no run.")
        return Opened(existing, run, deduped=True)

    merged = await find_recent_failed(session, workspace_id, group, now, is_test)
    prior_signals: list[dict[str, object]] = []
    for prior in merged:
        prior_signals.extend(prior.signals)
        unique_ids += [i for i in prior.affected_resource_ids if i not in unique_ids]
        group = sorted(set(group) | set(prior.group_resource_ids))
    # Detection is timed from the signal that opens this incident; a merged incident's earlier
    # signals may be an unrelated spike (a load test before the bad deploy).
    first_at = min(s.at for s in signals)
    rule: DetectorRule = signals[0].rule
    ranked: list[Severity] = sorted((_SEVERITY[s.rule] for s in signals), key=_RANK.__getitem__)
    level: Severity = severity if severity is not None else ranked[0]
    start = since or (
        first_at - LEAD_TIME
        if detected_by == "watcher"
        else now - timedelta(minutes=get_settings().incident_default_window_minutes)
    )
    if merged:
        start = min([start, *(p.window_start for p in merged)])
    incident = Incident(
        workspace_id=workspace_id,
        title=title or _title(signals, resources),
        status="open",
        severity=level,
        summary=description,
        detected_by=detected_by,
        detector_rule=rule,
        window_start=start,
        first_signal_at=first_at,
        affected_resource_ids=unique_ids,
        group_resource_ids=group,
        signals=[*prior_signals, *(s.model_dump(mode="json") for s in signals)][-MAX_SIGNALS:],
        # Kept by MedicState (extra fields): the incidents this one took over.
        state={"merged_from": [str(p.id) for p in merged]} if merged else {},
        is_test=is_test,
    )
    session.add(incident)
    await session.flush()
    run = Run(
        workspace_id=workspace_id,
        incident_id=incident.id,
        agent=MEDIC_AGENT,
        goal=f"Investigate: {incident.title}"[:4000],
        status=RunStatus.queued,
    )
    session.add(run)
    await session.flush()
    await record_step(
        session,
        run,
        kind="incident_opened",
        output_summary=incident.title,
        data=IncidentOpened(
            incident_id=incident.id,
            title=incident.title,
            severity=level,
            detected_by=detected_by,
            detector_rule=rule,
            signals=signals,
            affected_resources=[_ref(r) for r in resources]
            + [_ref(r) for r in await _merged_resources(session, unique_ids, resources)],
            window=TimeWindow(start=start),
            merged_from=[p.id for p in merged],
        ),
    )
    for prior in merged:
        # Its own status: the outage goes on in the newer incident, it did not "fail".
        previous = prior.status
        prior.status = "merged"
        prior.state = {**(prior.state or {}), "merged_into": str(incident.id)}
        prior_run = await latest_run(session, prior.id)
        if prior_run is not None:
            await record_step(
                session,
                prior_run,
                kind="incident_updated",
                output_summary=f"Merged into a newer incident: {incident.title}"[:300],
                data=IncidentUpdated(
                    incident_id=prior.id,
                    status="merged",
                    previous_status=previous if previous in get_args(IncidentStatus) else None,
                    at=now,
                    reason="The same outage showed up on a related resource.",
                    merged_into=incident.id,
                ),
            )
    await queue.enqueue(
        session,
        INVESTIGATE_JOB,
        {"incident_id": str(incident.id), "run_id": str(run.id)},
        max_attempts=1,
    )
    await session.commit()
    return Opened(incident, run, deduped=False)


async def set_status(
    session: AsyncSession,
    incident: Incident,
    run: Run,
    status: IncidentStatus,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> None:
    """Change the status and record `incident_updated` on the run. The caller commits."""
    previous = incident.status
    if previous == status:
        return
    now = now or datetime.now(UTC)
    incident.status = status
    if status in ("resolved", "failed"):
        incident.window_end = incident.window_end or now
        if status == "resolved":
            incident.resolved_at = now
    await record_step(
        session,
        run,
        kind="incident_updated",
        output_summary=f"{previous} -> {status}" + (f": {reason}" if reason else ""),
        data=IncidentUpdated(
            incident_id=incident.id,
            status=status,
            previous_status=previous if previous in get_args(IncidentStatus) else None,
            at=now,
            reason=reason,
        ),
    )


async def count_open(session: AsyncSession, workspace_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count(Incident.id)).where(
                Incident.workspace_id == workspace_id, Incident.status.in_(OPEN_STATUSES)
            )
        )
        or 0
    )


async def close_for_stopped_run(session: AsyncSession, run: Run, who: str) -> None:
    """A person stopped the incident's Medic run: the incident fails with that reason (its
    pending fix approvals were already rejected by the stop). The caller commits."""
    from app.agents.medic.events import IncidentFailed
    from app.agents.medic.state import MedicState, Outcome

    if run.incident_id is None or run.agent != MEDIC_AGENT:
        return
    incident = await session.get(Incident, run.incident_id, with_for_update=True)
    if incident is None or incident.status not in OPEN_STATUSES:
        return
    reason = f"Stopped by {who}."
    state = MedicState.model_validate(incident.state or {})
    state.phase, state.error = "done", reason
    state.outcome = Outcome(result="failed", reason=reason)
    for rec in state.fixes:
        if rec.status in ("proposed", "waiting_approval"):
            rec.status = "rejected"
    incident.state = state.model_dump(mode="json")
    await set_status(session, incident, run, "failed", reason=reason)
    await record_step(
        session,
        run,
        kind="incident_failed",
        output_summary=reason,
        data=IncidentFailed(incident_id=incident.id, reason=reason),
    )
