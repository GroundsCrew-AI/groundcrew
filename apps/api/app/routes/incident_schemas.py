"""Request and response models for /incidents (docs/phase5-interfaces.md).

An incident is investigated by a Medic run (`agent = "medic"`): follow it live on the run's
event stream (`POST /runs/{run_id}/stream-token`, `GET /runs/{run_id}/events`), approve its
fixes with `POST /approvals/{id}/decide`. These shapes do not change when the routes start
working (M4).
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.agents.medic.events import (
    DetectedBy,
    FixApplied,
    FixStatus,
    IncidentStatus,
    MemoryHit,
    ProposedFix,
    ResearchResult,
    ResourceRef,
    Severity,
    Signal,
)
from app.agents.medic.schemas import EvidenceRef, RootCause, TriggeringChange
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
    # Start of the window to investigate. Default: 60 minutes before the report.
    since: datetime | None = None


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
    title: str
    status: IncidentStatus
    severity: Severity
    detected_by: DetectedBy
    affected_resources: list[ResourceRef]
    # The run investigating it now (the latest Medic run for this incident).
    run_id: uuid.UUID | None
    window_start: datetime
    window_end: datetime | None
    root_cause_summary: str | None
    triggering_change: TriggeringChange | None
    # pull_request, ecs_rollback_service, ... for the fixes that were applied.
    fixes_applied: list[str]
    pull_requests: list[PullRequestRef]
    memory_reused: bool
    cost_usd: float | None
    ultra_equiv_usd: float | None = None
    created_at: datetime
    resolved_at: datetime | None
    # First signal -> incident opened, and opened -> resolved.
    time_to_detect_ms: int | None
    duration_ms: int | None


class IncidentPage(BaseModel):
    items: list[IncidentSummary]
    next_cursor: str | None


class FixState(BaseModel):
    """A proposed fix and what happened to it."""

    fix: ProposedFix
    status: FixStatus
    applied: FixApplied | None = None


class IncidentDetail(IncidentSummary):
    signals: list[Signal]
    root_cause: RootCause | None
    # Top ranked evidence lines (the full text stays in the run).
    evidence: list[EvidenceRef]
    similar_incidents: list[MemoryHit]
    research: list[ResearchResult]
    fixes: list[FixState]
    verify: VerifyResult | None
    # Every Medic run for this incident, oldest first (a re-investigation adds one).
    run_ids: list[uuid.UUID]
    error: str | None = None
