"""Request and response models for /incidents (docs/phase5-interfaces.md).

An incident is investigated by a Medic run (`agent = "medic"`): follow it live on the run's
event stream (`POST /runs/{run_id}/stream-token`, `GET /runs/{run_id}/events`), approve its
fixes with `POST /approvals/{id}/decide`. These shapes do not change when the routes start
working (M4).
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents.medic.events import (
    ChangeMarker,
    DetectedBy,
    DetectorRule,
    FixApplied,
    FixStatus,
    IncidentStatus,
    MemoryReuse,
    ProposedFix,
    ResearchResult,
    ResourceRef,
    Severity,
    Signal,
    TimeWindow,
)
from app.agents.medic.schemas import EvidenceItem, RootCause, TriggeringChange
from app.pipeline.events import VerifyResult
from app.tools.github_pr import PullRequestRef

MAX_INCIDENT_RESOURCES = 20

# --- request ------------------------------------------------------------------------------


class IncidentCreate(BaseModel):
    """ "Report incident": a person (or the watcher) names what is broken."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: uuid.UUID
    # Graph resources (GET /workspaces/{id}/graph) that look broken.
    resource_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_INCIDENT_RESOURCES)
    title: str | None = Field(default=None, max_length=300)
    # What the person sees, in their words. Goes into the investigation as a symptom.
    description: str | None = Field(default=None, max_length=4000)
    severity: Severity | None = None
    # Start of the window to investigate. Default: 60 minutes before the report.
    since: datetime | None = None
    # A test or demo report: Medic runs as usual, but the incident never enters incident
    # memory and is never deduped or merged with real incidents.
    test: bool = False


class IncidentCreated(BaseModel):
    incident_id: uuid.UUID
    run_id: uuid.UUID
    # True when an open incident already covered these resources: its ids are returned and
    # no new incident is opened.
    deduped: bool = False


# --- responses ----------------------------------------------------------------------------


class IncidentSummary(BaseModel):
    """One row of GET /incidents."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    # The run investigating it now (the latest Medic run for this incident).
    run_id: uuid.UUID | None
    title: str
    status: IncidentStatus
    severity: Severity
    detected_by: DetectedBy
    # Reported with test: true; kept out of incident memory.
    test: bool = False
    # The detector rule that fired first (user_report for a person's report).
    detector_rule: DetectorRule
    opened_at: datetime
    resolved_at: datetime | None
    window: TimeWindow
    # Map deep links: affected_resource_ids, with names and types in affected_resources.
    affected_resource_ids: list[uuid.UUID]
    affected_resources: list[ResourceRef]
    root_cause_summary: str | None
    triggering_change: TriggeringChange | None
    # pull_request, ecs_rollback_service, ... for the fixes that were applied.
    fixes_applied: list[str]
    pull_requests: list[PullRequestRef]
    memory_reused: bool
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float | None
    ultra_equiv_usd: float | None = None
    # First signal -> incident opened, and opened -> resolved.
    time_to_detect_ms: int | None
    duration_ms: int | None


class IncidentPage(BaseModel):
    items: list[IncidentSummary]
    # Pass as ?cursor= for the next page. Null when there are no more.
    next_cursor: str | None


class FixState(BaseModel):
    """A proposed fix and what happened to it. `fix.kind` is pr or emergency;
    `fix.plan_lines` is the plan as from -> to lines; the PR's URL and state are in
    `pull_request` once opened."""

    fix: ProposedFix
    status: FixStatus
    approval_id: uuid.UUID | None
    pull_request: PullRequestRef | None = None
    applied: FixApplied | None = None


class MemoryUse(BaseModel):
    incident_id: uuid.UUID
    title: str
    similarity: float
    reused: list[MemoryReuse]


class IncidentOutcome(BaseModel):
    result: Literal["resolved", "failed"]
    reason: str | None = None
    # Signals that did not recover (failed only).
    still_failing: list[str] = Field(default_factory=list)


class IncidentDetail(IncidentSummary):
    signals: list[Signal]
    # The ranked evidence lines; root_cause claims cite them by id.
    evidence: list[EvidenceItem]
    # Deploys, commits, and secret changes for the timeline chart.
    changes: list[ChangeMarker]
    root_cause: RootCause | None
    fixes: list[FixState]
    # The last recovery check (the shared VerifyResult), and the final outcome once there is one.
    verify: VerifyResult | None
    outcome: IncidentOutcome | None
    # The past incident that was reused, if any.
    memory: MemoryUse | None
    research: list[ResearchResult]
    # Every Medic run for this incident, oldest first (a re-investigation adds one).
    run_ids: list[uuid.UUID]
    error: str | None = None
