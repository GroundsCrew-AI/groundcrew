"""Typed payloads for incident run events (docs/phase5-interfaces.md, docs/events.md).

Medic investigates an incident in a run with `agent = "medic"` and `runs.incident_id` set. Each
event is a run step: `run_steps.kind` is the event name and `run_steps.data` one of the payloads
below, discriminated by `type` (equal to the step kind). They stream on the run's SSE exactly
like deploy events, so Last-Event-ID resume and page reloads work the same way.

Approvals reuse `approval_requested` / `approval_decided`; Medic's requested step carries a
`fix_approval` payload (the exact fix to approve). Recovery checks reuse the deploy
`verify_result` payload (`app.pipeline.events.VerifyResult`). Model calls stay `llm` steps.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.actions.base import ActionPlan, ActionResult, EmergencyActionName
from app.agents.medic.schemas import ChangeKind, EvidenceItem, RootCause
from app.tools.github_pr import PullRequestRef

# Event names an incident run adds to the run stream (besides step, approval_requested,
# approval_decided, verify_result, status, done).
MEDIC_EVENT_NAMES = (
    "incident_opened",
    "incident_updated",
    "evidence_collected",
    "memory_checked",
    "research_done",
    "diagnosis",
    "fix_proposed",
    "fix_applied",
    "incident_resolved",
    "incident_failed",
)

IncidentStatus = Literal[
    "open",  # reported; Medic not started yet
    "investigating",  # gathering, memory, research, diagnosis, proposing fixes
    "waiting_approval",  # at least one fix waits for a person
    "applying",  # an approved fix is running
    "verifying",  # re-checking the signals that opened it
    "resolved",  # signals recovered
    "failed",  # gave up: verify timed out, every fix rejected, budget, or an error
]
Severity = Literal["critical", "high", "medium", "low"]
DetectedBy = Literal["watcher", "user"]
# Detector rules (M2). user_report: POST /incidents from a person.
DetectorRule = Literal[
    "health_check",  # api health failures (ECS container health or an HTTP check)
    "ecs_deployment_failed",
    "ecs_tasks_stopped",  # tasks stopped non-zero (exit 137 / OOM included)
    "log_error_spike",
    "rds_cpu_latency",
    "user_report",
    "other",
]


class TimeWindow(BaseModel):
    start: datetime
    # Null while the incident is open: the window runs to now.
    end: datetime | None = None


class ResourceRef(BaseModel):
    """A graph resource; `id` is what the Map deep-links to."""

    id: uuid.UUID
    name: str
    type: str = Field(examples=["ecs:service", "rds:db", "secretsmanager:secret"])
    provider: str


class Signal(BaseModel):
    """A detector rule that fired, or what verify re-checks."""

    rule: DetectorRule
    resource_id: uuid.UUID | None = None
    resource_name: str | None = None
    detail: str = Field(examples=["3 tasks stopped with exit code 137 (OutOfMemoryError)"])
    at: datetime


class ChangeMarker(BaseModel):
    """A change in or before the window, for the timeline chart."""

    kind: ChangeKind
    # Commit SHA, deploy run id or task definition ARN, secret version id.
    ref: str
    at: datetime
    resource_id: uuid.UUID | None = None
    label: str = Field(examples=["abc1234 Lower worker memory", "checkout/database-url changed"])
    url: str | None = None
    # GitHub changes: the full commit SHA (a deploy's head commit) and the owner/name repo.
    commit: str | None = None
    repo: str | None = Field(default=None, examples=["acme/checkout"])


class IncidentOpened(BaseModel):
    type: Literal["incident_opened"] = "incident_opened"
    incident_id: uuid.UUID
    title: str
    severity: Severity
    detected_by: DetectedBy
    # The detector rule that fired first (user_report for a person's report).
    detector_rule: DetectorRule
    signals: list[Signal]
    affected_resources: list[ResourceRef]
    window: TimeWindow
    # Recently failed incidents on related resources that this one took over (their signals
    # and resources are part of it): one outage, one incident.
    merged_from: list[uuid.UUID] = Field(default_factory=list)


FollowUpKind = Literal["fix_pr"]
# open: the fix PR is not merged. merged: merged, its deploy not seen yet. deployed: a successful
# deploy run contains the merge (done). closed_unmerged: closed without merging; the running
# service is only on the emergency rollback and the next deploy brings the failure back.
FollowUpState = Literal["open", "merged", "closed_unmerged", "deployed"]
PENDING_FOLLOWUP_STATES: tuple[FollowUpState, ...] = ("open", "merged")


class FollowUp(BaseModel):
    """Work left after an incident resolved: its permanent fix PR, until it is deployed."""

    id: uuid.UUID
    kind: FollowUpKind = "fix_pr"
    state: FollowUpState
    repo: str = Field(examples=["GroundsCrew-AI/checkout-demo"])
    number: int
    url: str
    # The commit that caused the incident; still in the repo until the PR merges.
    triggering_commit: str | None = None
    merge_commit: str | None = None
    merged_at: datetime | None = None
    deployed_at: datetime | None = None
    # The workflow run that deployed the merge.
    deploy_url: str | None = None
    closed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    # Checked through the GitHub App until then (7 days after it was created).
    tracked_until: datetime


class IncidentUpdated(BaseModel):
    """Every status change, so the UI can update the incident header without a reload."""

    type: Literal["incident_updated"] = "incident_updated"
    incident_id: uuid.UUID
    status: IncidentStatus
    previous_status: IncidentStatus | None
    at: datetime
    reason: str | None = None
    # Set on a failed incident when a newer one took it over (IncidentOpened.merged_from).
    merged_into: uuid.UUID | None = None
    # Set when follow-ups were created or changed (the status may stay "resolved"): all of the
    # incident's follow-ups, as they are now.
    followups: list[FollowUp] | None = None


class EvidenceSourceSummary(BaseModel):
    source: str = Field(examples=["cloudwatch_logs:/ecs/checkout-worker"])
    lines: int
    truncated: bool


class EvidenceCollected(BaseModel):
    type: Literal["evidence_collected"] = "evidence_collected"
    sources: list[EvidenceSourceSummary]
    # Resources added by following graph edges from the affected ones (uses, logs_to, ...).
    neighbors: list[ResourceRef] = Field(default_factory=list)
    # The ranked lines the diagnosis may cite (at most about 40), with stable ids.
    items: list[EvidenceItem]
    changes: list[ChangeMarker] = Field(default_factory=list)
    total_lines: int


class MemoryHit(BaseModel):
    incident_id: uuid.UUID
    title: str
    similarity: float
    root_cause: str | None
    fix_summary: str | None
    resolved_at: datetime | None


MemoryReuse = Literal["diagnosis", "fix", "research"]


class MemoryChecked(BaseModel):
    type: Literal["memory_checked"] = "memory_checked"
    hits: list[MemoryHit]
    # The hit Medic reused (the closest one above the threshold), and what it reused. The
    # diagnosis is still checked against this incident's own evidence.
    reused_incident_id: uuid.UUID | None = None
    reused: list[MemoryReuse] = Field(default_factory=list)


class ResearchResult(BaseModel):
    title: str
    url: str
    snippet: str


class ResearchDone(BaseModel):
    type: Literal["research_done"] = "research_done"
    queries: list[str]
    # At most 3 kept, with URLs.
    results: list[ResearchResult]
    # Set when research did not run: "memory hit", "cap reached", "no Tavily key", ...
    skipped_reason: str | None = None


class Diagnosis(BaseModel):
    type: Literal["diagnosis"] = "diagnosis"
    # Only cited claims: ids not in the collected evidence were removed, and claims left with
    # none were dropped.
    root_cause: RootCause
    # What the post-check removed, one line each, so the UI can show "2 uncited claims dropped".
    dropped: list[str] = Field(default_factory=list)


class FileChange(BaseModel):
    path: str
    # Unified diff against the base branch (whole file for a new file).
    diff: str


class PullRequestView(BaseModel):
    """A fix PR as shown before approval (the draft; contents as diffs)."""

    repo: str
    base: str
    branch: str
    title: str
    body: str
    files: list[FileChange]


class PlanLine(BaseModel):
    """One human-readable line of an ActionPlan: what changes, from what, to what."""

    what: str = Field(examples=["checkout-worker task definition"])
    before: str | None = Field(examples=["checkout-worker:42"])
    after: str | None = Field(examples=["checkout-worker:41"])


FixKind = Literal["pr", "emergency"]
FixStatus = Literal["proposed", "waiting_approval", "rejected", "applying", "applied", "failed"]


class ProposedFix(BaseModel):
    fix_id: str = Field(examples=["fix-1"])
    kind: FixKind
    # 1 runs first. An emergency action (production down now) comes before its PR.
    order: int
    title: str
    rationale: str
    # kind = emergency
    action: EmergencyActionName | None = None
    plan: ActionPlan | None = None
    plan_lines: list[PlanLine] = Field(default_factory=list)
    # kind = pr
    pull_request: PullRequestView | None = None
    approval_id: uuid.UUID | None = None
    # Fixes (fix_id) that must be applied first, e.g. a restart needs the secret rollback: it
    # cannot be approved before they are applied, and is not run if one is rejected or fails.
    depends_on: list[str] = Field(default_factory=list)


class FixProposed(BaseModel):
    type: Literal["fix_proposed"] = "fix_proposed"
    fixes: list[ProposedFix]


class FixApproval(BaseModel):
    """On Medic's `approval_requested` step: the one fix this approval runs."""

    type: Literal["fix_approval"] = "fix_approval"
    incident_id: uuid.UUID
    fix: ProposedFix


class FixApplied(BaseModel):
    type: Literal["fix_applied"] = "fix_applied"
    fix_id: str
    kind: FixKind
    ok: bool
    action_result: ActionResult | None = None
    pull_request: PullRequestRef | None = None
    error: str | None = None


class IncidentResolved(BaseModel):
    type: Literal["incident_resolved"] = "incident_resolved"
    incident_id: uuid.UUID
    # Detection: first signal -> incident opened. Resolution: opened -> verified healthy.
    time_to_detect_ms: int | None
    duration_ms: int
    fixes_applied: list[str]
    pull_requests: list[PullRequestRef] = Field(default_factory=list)
    memory_reused: bool
    cost_usd: float | None
    ultra_equiv_usd: float | None = None


class IncidentFailed(BaseModel):
    type: Literal["incident_failed"] = "incident_failed"
    incident_id: uuid.UUID
    reason: str
    # Signals that did not recover, one line each.
    still_failing: list[str] = Field(default_factory=list)
    cost_usd: float | None = None


MedicEventData = Annotated[
    IncidentOpened
    | IncidentUpdated
    | EvidenceCollected
    | MemoryChecked
    | ResearchDone
    | Diagnosis
    | FixProposed
    | FixApproval
    | FixApplied
    | IncidentResolved
    | IncidentFailed,
    Field(discriminator="type"),
]
