"""What Medic keeps on the incident (`incidents.state`): the investigation's results, the fixes
and their approvals, and the recovery checks. It survives pauses for approval and worker
restarts, and the incidents API (M4) reads it as is.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents.medic.events import (
    ChangeMarker,
    EvidenceSourceSummary,
    FixApplied,
    FixStatus,
    MemoryHit,
    MemoryReuse,
    ProposedFix,
    ResearchResult,
    ResourceRef,
)
from app.agents.medic.schemas import EvidenceItem, RootCause
from app.pipeline.events import VerifyResult
from app.tools.github_pr import PullRequestDraft, PullRequestRef

Phase = Literal["investigating", "waiting_approval", "applying", "verifying", "done"]


class FixRecord(BaseModel):
    fix: ProposedFix
    status: FixStatus = "proposed"
    approval_id: uuid.UUID | None = None
    # The full PR draft (file contents) the approval binds; the card shows its diffs.
    draft: PullRequestDraft | None = None
    pull_request: PullRequestRef | None = None
    applied: FixApplied | None = None


class MemoryUsed(BaseModel):
    incident_id: uuid.UUID
    title: str
    similarity: float
    reused: list[MemoryReuse]


class Outcome(BaseModel):
    result: Literal["resolved", "failed"]
    reason: str | None = None
    still_failing: list[str] = Field(default_factory=list)


class MedicState(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: uuid.UUID | None = None
    phase: Phase = "investigating"
    sources: list[EvidenceSourceSummary] = Field(default_factory=list)
    neighbors: list[ResourceRef] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    changes: list[ChangeMarker] = Field(default_factory=list)
    memory_hits: list[MemoryHit] = Field(default_factory=list)
    memory: MemoryUsed | None = None
    research: list[ResearchResult] = Field(default_factory=list)
    research_queries: list[str] = Field(default_factory=list)
    tavily_calls: int = 0
    root_cause: RootCause | None = None
    dropped: list[str] = Field(default_factory=list)
    fixes: list[FixRecord] = Field(default_factory=list)
    # Things a person should know that are not fixes ("no Operate role: rollback not offered").
    notes: list[str] = Field(default_factory=list)
    fixes_applied_at: datetime | None = None
    verify: VerifyResult | None = None
    verify_deadline: datetime | None = None
    healthy_streak: int = 0
    # One verify chain per incident: jobs carrying another chain id stop (a second resume
    # used to start a second chain, so every round showed up twice).
    verify_chain: str | None = None
    verify_checks: int = 0
    # Re-diagnoses after new signals or a rejected "false alarm" (at most MAX_REDIAGNOSES).
    rediagnosed: int = 0
    # "No fix" waits until then for related signals (another service of the same outage) to
    # join the incident before it fails; new material signals re-diagnose it instead.
    no_fix_until: datetime | None = None
    # Fixes waiting for approval when new signals started a re-diagnosis. Their approvals stay
    # pending: kept when the re-diagnosis proposes the same fixes, closed when it does not.
    superseded: list[FixRecord] = Field(default_factory=list)
    # The application's stack for research queries ("postgres", "asyncpg", "sqlalchemy").
    stack: list[str] = Field(default_factory=list)
    no_fix_waited: bool = False
    outcome: Outcome | None = None
    error: str | None = None
