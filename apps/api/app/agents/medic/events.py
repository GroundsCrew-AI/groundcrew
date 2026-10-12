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
from app.agents.medic.schemas import EvidenceRef, RootCause
from app.tools.github_pr import PullRequestRef

# Event names an incident run adds to the run stream (besides step, approval_requested,
# approval_decided, verify_result, status, done).
MEDIC_EVENT_NAMES = (
    "incident_opened",
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
SignalKind = Literal[
    "health_check",  # api health failures (ECS container health or an HTTP check)
    "ecs_deployment_failed",
    "ecs_tasks_stopped",  # tasks stopped non-zero (exit 137 / OOM included)
    "log_error_spike",
    "rds_cpu_latency",
    "user_report",
    "other",
]


class ResourceRef(BaseModel):
    id: uuid.UUID
    name: str
    type: str = Field(examples=["ecs:service", "rds:db", "secretsmanager:secret"])
    provider: str


class Signal(BaseModel):
    """Why an incident was opened, or what verify re-checks."""

    kind: SignalKind
    resource_id: uuid.UUID | None = None
    resource_name: str | None = None
    detail: str = Field(examples=["3 tasks stopped with exit code 137 (OutOfMemoryError)"])
    at: datetime


class IncidentOpened(BaseModel):
    type: Literal["incident_opened"] = "incident_opened"
    incident_id: uuid.UUID
    title: str
    severity: Severity
    detected_by: DetectedBy
    signals: list[Signal]
    affected_resources: list[ResourceRef]
    window_start: datetime
    window_end: datetime | None = None


class EvidenceSourceSummary(BaseModel):
    source: str = Field(examples=["cloudwatch_logs:/ecs/checkout-worker"])
    lines: int
    truncated: bool


class EvidenceCollected(BaseModel):
    type: Literal["evidence_collected"] = "evidence_collected"
    sources: list[EvidenceSourceSummary]
    # Resources added by following graph edges from the affected ones (uses, logs_to, ...).
    neighbors: list[ResourceRef] = Field(default_factory=list)
    # The most relevant lines after ranking (at most about 12), shown on the card.
    top: list[EvidenceRef]
    total_lines: int


class MemoryHit(BaseModel):
    incident_id: uuid.UUID
    title: str
    similarity: float
    root_cause: str | None
    fix_summary: str | None
    resolved_at: datetime | None


class MemoryChecked(BaseModel):
    type: Literal["memory_checked"] = "memory_checked"
    hits: list[MemoryHit]
    # True when a past incident was close enough that Medic reuses its diagnosis and fix
    # (a faster path; the diagnosis is still checked against this incident's evidence).
    reused: bool


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
    # Only cited claims: evidence lines not found in the collected evidence were dropped.
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


FixKindOut = Literal["pull_request", "emergency_action"]
FixStatus = Literal["proposed", "waiting_approval", "rejected", "applying", "applied", "failed"]


class ProposedFix(BaseModel):
    fix_id: str = Field(examples=["fix-1"])
    kind: FixKindOut
    # 1 runs first. An emergency action (production down now) comes before its PR.
    order: int
    title: str
    rationale: str
    # kind = emergency_action
    action: EmergencyActionName | None = None
    plan: ActionPlan | None = None
    # kind = pull_request
    pull_request: PullRequestView | None = None
    approval_id: uuid.UUID | None = None


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
    kind: FixKindOut
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
