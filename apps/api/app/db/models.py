"""SQLAlchemy models for every Groundcrew table (PROJECT_CONTEXT.md sections 8 and 9)."""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import get_settings

# Stable constraint names so Alembic migrations stay predictable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

JSON = dict[str, Any]


class RunStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    waiting_approval = "waiting_approval"
    succeeded = "succeeded"
    failed = "failed"
    stopped = "stopped"


TERMINAL_RUN_STATUSES = frozenset({RunStatus.succeeded, RunStatus.failed, RunStatus.stopped})


class StopReason(enum.StrEnum):
    completed = "completed"
    max_steps = "max_steps"
    budget = "budget"
    # Builder repeated the same kind of fix for the same build error (Phase 4).
    no_progress = "no_progress"
    waiting_approval = "waiting_approval"
    error = "error"
    stopped_by_user = "stopped_by_user"


class ApprovalStatus(enum.StrEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class ToolPolicy(enum.StrEnum):
    read = "read"
    write = "write"
    emergency = "emergency"


class WorkspaceRole(enum.StrEnum):
    owner = "owner"
    member = "member"


class JobStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


def _pg_enum(enum_cls: type[enum.StrEnum], name: str) -> Enum:
    return Enum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e])


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {JSON: JSONB}


class TimestampMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    # GitHub's numeric user id: stable across login renames. Users are upserted by it.
    github_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    # GitHub may not share an email, so it is optional.
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    name: Mapped[str | None] = mapped_column(String(200))
    github_login: Mapped[str | None] = mapped_column(String(100), unique=True)
    avatar_url: Mapped[str | None] = mapped_column(String(500))


class Workspace(TimestampMixin, Base):
    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(200))
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    settings: Mapped[JSON] = mapped_column(default=dict, server_default="{}")


class WorkspaceMember(TimestampMixin, Base):
    """Who can see a workspace. Every workspace-scoped route checks this table."""

    __tablename__ = "workspace_members"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_members_workspace_user"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[WorkspaceRole] = mapped_column(
        _pg_enum(WorkspaceRole, "workspace_role"), default=WorkspaceRole.member
    )


class Connection(TimestampMixin, Base):
    """A customer account or system we connect to (AWS, Azure, GitHub, cluster, VM)."""

    __tablename__ = "connections"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    # aws | azure | github | k8s | vm | vercel
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(200))
    # Non-secret settings: role ARN, external ID, subscription ID, host, ...
    config: Mapped[JSON] = mapped_column(default=dict, server_default="{}")
    # Fernet token (app.security.crypto). Never store plaintext secrets.
    secret_encrypted: Mapped[str | None] = mapped_column(Text)
    # pending | connected | error
    status: Mapped[str] = mapped_column(String(32), default="pending", server_default="pending")
    # The last discovery of this connection: {at, run_id, status, resources, stale, error,
    # duration_ms, notes}. Empty until it has run once.
    last_discovery: Mapped[JSON] = mapped_column(default=dict, server_default="{}")


class Resource(TimestampMixin, Base):
    """Normalized cloud resource. Every provider maps into this shape."""

    __tablename__ = "resources"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "provider", "external_id", name="uq_resources_workspace_external"
        ),
        Index("ix_resources_workspace_provider_type", "workspace_id", "provider", "type"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("connections.id", ondelete="SET NULL"), index=True
    )
    # aws | azure | gcp | k8s | vercel | vm
    provider: Mapped[str] = mapped_column(String(16))
    type: Mapped[str] = mapped_column(String(200))
    # Provider ID (ARN, Azure resource ID, k8s uid, ...). Stable across discoveries.
    external_id: Mapped[str] = mapped_column(Text)
    # AWS account ID, Azure subscription, k8s cluster, Vercel team, GitHub owner.
    account: Mapped[str | None] = mapped_column(String(200))
    region: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(500))
    tags: Mapped[JSON] = mapped_column(default=dict, server_default="{}")
    config: Mapped[JSON] = mapped_column(default=dict, server_default="{}")
    # observed | diagnosable | actionable
    coverage: Mapped[str] = mapped_column(String(16), default="observed", server_default="observed")
    iac_path: Mapped[str | None] = mapped_column(Text)
    repo: Mapped[str | None] = mapped_column(String(500))
    # Not seen by the last discovery of its connection. Kept (with its edges) until removed.
    stale: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Edge(TimestampMixin, Base):
    __tablename__ = "edges"
    __table_args__ = (UniqueConstraint("from_id", "to_id", "kind", name="uq_edges_from_to_kind"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    from_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resources.id", ondelete="CASCADE"))
    to_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), index=True
    )
    # calls | reads | deployed_by | defined_in, plus the extensions uses | pulls_from |
    # logs_to (app.discovery.types.EdgeKind)
    kind: Mapped[str] = mapped_column(String(32))
    # high | medium | low
    confidence: Mapped[str] = mapped_column(String(8), default="medium", server_default="medium")
    # {"text": one line a person can check, "rule": which inference rule made it, ...}.
    # Never holds secret values.
    evidence: Mapped[JSON] = mapped_column(default=dict, server_default="{}")


class Incident(TimestampMixin, Base):
    __tablename__ = "incidents"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(32), default="open", server_default="open")
    severity: Mapped[str | None] = mapped_column(String(16))
    summary: Mapped[str | None] = mapped_column(Text)
    root_cause: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(get_settings().embed_dim))


class Run(TimestampMixin, Base):
    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_workspace_created", "workspace_id", "created_at"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    incident_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("incidents.id", ondelete="SET NULL"), index=True
    )
    agent: Mapped[str] = mapped_column(String(64))
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[RunStatus] = mapped_column(
        _pg_enum(RunStatus, "run_status"), default=RunStatus.queued, index=True
    )
    stop_reason: Mapped[StopReason | None] = mapped_column(_pg_enum(StopReason, "stop_reason"))
    # Conversation so far, in OpenAI chat format. Lets a run pause and resume.
    messages: Mapped[list[JSON]] = mapped_column(JSONB, default=list, server_default="[]")
    # Agent state that must survive a pause for approval. Deploy runs: app.pipeline.state.
    state: Mapped[JSON] = mapped_column(JSONB, default=dict, server_default="{}")
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    # What the same tokens would have cost on Ultra. cost_usd / ultra_equiv_usd shows the saving.
    ultra_equiv_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class RunStep(TimestampMixin, Base):
    __tablename__ = "run_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_run_steps_run_seq"),
        # Daily budget sums tokens by day.
        Index("ix_run_steps_created_at", "created_at"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    agent: Mapped[str] = mapped_column(String(64))
    # llm | tool | approval_requested | approval_decided | budget | stopped | error, and the
    # deploy events in app.pipeline.events (stage_started, build_attempt, ...).
    kind: Mapped[str] = mapped_column(String(32))
    model_tier: Mapped[str | None] = mapped_column(String(16))
    model_id: Mapped[str | None] = mapped_column(String(200))
    tool_name: Mapped[str | None] = mapped_column(String(100))
    # Truncated and redacted. Never the full prompt.
    input_summary: Mapped[str | None] = mapped_column(Text)
    output_summary: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    # LLM steps: reasoning mode sent (off | low | on), Ultra-equivalent cost, whether the call
    # went to the route's fallback tier or was escalated one tier, and the start of the
    # reasoning trace (redacted, truncated). The trace is shown, never sent back to the model.
    reasoning_mode: Mapped[str | None] = mapped_column(String(8))
    ultra_equiv_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    fell_back: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    escalated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    reasoning_excerpt: Mapped[str | None] = mapped_column(Text)
    # Set on approval_requested / approval_decided steps, so the timeline can show the approval.
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("approvals.id", ondelete="SET NULL", use_alter=True)
    )
    # Typed event payload for deploy steps (app.pipeline.events), redacted. Null otherwise.
    data: Mapped[JSON | None] = mapped_column(JSONB)


class Approval(TimestampMixin, Base):
    __tablename__ = "approvals"
    __table_args__ = (
        Index("ix_approvals_status_created", "status", "created_at"),
        UniqueConstraint("run_id", "tool_call_id", name="uq_approvals_run_tool_call"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    tool_call_id: Mapped[str] = mapped_column(String(100))
    tool_name: Mapped[str] = mapped_column(String(100))
    policy: Mapped[ToolPolicy] = mapped_column(_pg_enum(ToolPolicy, "tool_policy"))
    # Exact validated arguments. Only these are executed after approval.
    arguments: Mapped[JSON] = mapped_column(default=dict)
    status: Mapped[ApprovalStatus] = mapped_column(
        _pg_enum(ApprovalStatus, "approval_status"), default=ApprovalStatus.pending
    )
    # The user who approved or rejected it (section 7 rule 8).
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set once the approved action ran, so an approval can never be used twice.
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set atomically when an ApprovedAction for this approval is consumed by a deploy target
    # (UPDATE ... WHERE used_at IS NULL), so a token works once across workers and restarts.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Event(TimestampMixin, Base):
    """Normalized telemetry timeline entry (deploys, alarms, log spikes, config changes)."""

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_workspace_occurred", "workspace_id", "occurred_at"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resources.id", ondelete="SET NULL"), index=True
    )
    incident_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("incidents.id", ondelete="SET NULL"), index=True
    )
    source: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str | None] = mapped_column(String(16))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[JSON] = mapped_column(default=dict, server_default="{}")


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"
    # Matches the claim query in app.queue.
    __table_args__ = (Index("ix_jobs_status_run_after", "status", "run_after"),)

    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[JSON] = mapped_column(default=dict, server_default="{}")
    status: Mapped[JobStatus] = mapped_column(
        _pg_enum(JobStatus, "job_status"), default=JobStatus.queued
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(200))
    last_error: Mapped[str | None] = mapped_column(Text)


class LlmUsage(Base):
    """Token Factory spend per UTC day, across all runs. Backs the daily and total USD caps."""

    __tablename__ = "llm_usage"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0, server_default="0")
    tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
