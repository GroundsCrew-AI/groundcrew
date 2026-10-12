"""Request and response models for the API. The web app's TypeScript client is generated
from these (make api-types), so field names and types are the contract.

runs.messages (the raw conversation) is never returned: it can hold full prompts.
"""

import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from app.agents.medic.events import MedicEventData
from app.db.models import (
    Approval,
    ApprovalStatus,
    Run,
    RunStatus,
    RunStep,
    StopReason,
    ToolPolicy,
    User,
    Workspace,
    WorkspaceRole,
)
from app.pipeline.events import DeployEventData
from app.policy.engine import Decision
from app.security.redact import redact_json
from app.tools.registry import ToolNotFoundError, registry


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    github_login: str | None
    name: str | None
    email: str | None
    avatar_url: str | None


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    name: str
    role: WorkspaceRole

    @classmethod
    def build(cls, workspace: Workspace, role: WorkspaceRole) -> "WorkspaceOut":
        return cls(id=workspace.id, name=workspace.name, role=role)


class MeOut(BaseModel):
    user: UserOut
    workspaces: list[WorkspaceOut]


class RunCreate(BaseModel):
    agent: str = Field(examples=["echo_ops"])
    goal: str = Field(min_length=1, max_length=4000)
    workspace_id: uuid.UUID


class RunCreated(BaseModel):
    id: uuid.UUID
    status: RunStatus


def _optional_in_schema(*names: str) -> Callable[[dict[str, Any]], None]:
    """Keep fields added in Phase 4 out of the OpenAPI `required` list. The API always sends
    them, but the web app's hand-written fixtures do not have them yet, so marking them
    required would break its typecheck. TODO(frontend): remove once the web app uses them
    (see docs/api-changes.md)."""

    def drop(schema: dict[str, Any]) -> None:
        schema["required"] = [n for n in schema.get("required", []) if n not in names]
        # openapi-typescript makes any property with a default non-optional, so drop it too.
        for name in names:
            schema.get("properties", {}).get(name, {}).pop("default", None)

    return drop


_PHASE4_STEP_FIELDS = (
    "reasoning_mode",
    "ultra_equiv_usd",
    "fell_back",
    "escalated",
    "reasoning_excerpt",
    "data",
)

# Typed step payloads: deploy events (app.pipeline.events) and incident events
# (app.agents.medic.events). Their `type` values do not overlap.
RunEventData = DeployEventData | MedicEventData

_EVENT_DATA: TypeAdapter[RunEventData] = TypeAdapter(RunEventData)


def _event_data(raw: dict[str, Any] | None) -> RunEventData | None:
    if raw is None:
        return None
    try:
        return _EVENT_DATA.validate_python(raw)
    except ValidationError:
        # A payload this API version does not know (or a malformed one): keep the step.
        return None


def _tool_policy(tool_name: str | None) -> ToolPolicy | None:
    if tool_name is None:
        return None
    try:
        return registry.get(tool_name).policy
    except ToolNotFoundError:
        return None


class StepOut(BaseModel):
    model_config = ConfigDict(json_schema_extra=_optional_in_schema(*_PHASE4_STEP_FIELDS))

    id: uuid.UUID
    seq: int
    agent: str
    kind: str
    model_tier: str | None
    model_id: str | None
    tool_name: str | None
    tool_policy: ToolPolicy | None
    approval_id: uuid.UUID | None
    input_summary: str | None
    output_summary: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int | None
    cost_usd: float | None
    # LLM steps: reasoning mode sent (off | low | on), what the same tokens would cost on Ultra,
    # whether the route's fallback tier answered or the call was escalated one tier, and the
    # start of the model's reasoning trace (redacted, at most 2,000 characters).
    reasoning_mode: str | None = None
    ultra_equiv_usd: float | None = None
    fell_back: bool = False
    escalated: bool = False
    reasoning_excerpt: str | None = None
    # Deploy and incident steps: the typed event payload, discriminated by `type`
    # (docs/events.md).
    data: RunEventData | None = None
    created_at: datetime

    @classmethod
    def build(cls, step: RunStep) -> "StepOut":
        return cls(
            id=step.id,
            seq=step.seq,
            agent=step.agent,
            kind=step.kind,
            model_tier=step.model_tier,
            model_id=step.model_id,
            tool_name=step.tool_name,
            tool_policy=_tool_policy(step.tool_name),
            approval_id=step.approval_id,
            input_summary=step.input_summary,
            output_summary=step.output_summary,
            prompt_tokens=step.prompt_tokens,
            completion_tokens=step.completion_tokens,
            latency_ms=step.latency_ms,
            cost_usd=float(step.cost_usd) if step.cost_usd is not None else None,
            reasoning_mode=step.reasoning_mode,
            ultra_equiv_usd=_float(step.ultra_equiv_usd),
            fell_back=step.fell_back,
            escalated=step.escalated,
            reasoning_excerpt=step.reasoning_excerpt,
            data=_event_data(step.data),
            created_at=step.created_at,
        )


def _float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _duration_ms(run: Run) -> int | None:
    if run.started_at is None:
        return None
    end = run.finished_at
    if end is None:
        return None
    return round((end - run.started_at).total_seconds() * 1000)


class RunOut(BaseModel):
    """A run with its totals. duration_ms is set once the run has finished."""

    model_config = ConfigDict(
        from_attributes=True, json_schema_extra=_optional_in_schema("ultra_equiv_usd")
    )

    id: uuid.UUID
    workspace_id: uuid.UUID
    agent: str
    goal: str
    status: RunStatus
    stop_reason: StopReason | None
    result: str | None
    error: str | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float | None
    # What the same tokens would have cost on Ultra alone.
    ultra_equiv_usd: float | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    # Actions a person rejected in this run. They did not run, even if the run succeeded.
    rejected_actions: int = 0

    @classmethod
    def build(cls, run: Run, *, rejected_actions: int = 0) -> "RunOut":
        return cls(
            id=run.id,
            workspace_id=run.workspace_id,
            agent=run.agent,
            goal=run.goal,
            status=run.status,
            stop_reason=run.stop_reason,
            result=run.result,
            error=run.error,
            prompt_tokens=run.prompt_tokens,
            completion_tokens=run.completion_tokens,
            total_tokens=run.total_tokens,
            cost_usd=float(run.cost_usd) if run.cost_usd is not None else None,
            ultra_equiv_usd=_float(run.ultra_equiv_usd),
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            duration_ms=_duration_ms(run),
            rejected_actions=rejected_actions,
        )


class RunPage(BaseModel):
    items: list[RunOut]
    # Pass as ?cursor= to get the next page. Null when there are no more runs.
    next_cursor: str | None


class ApprovalOut(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    workspace_id: uuid.UUID
    run_agent: str
    run_goal: str
    tool_name: str
    policy: ToolPolicy
    # Redacted for display. The stored arguments, which are what runs, are not changed.
    arguments: dict[str, Any]
    status: ApprovalStatus
    decided_by: uuid.UUID | None
    decided_by_login: str | None
    decided_by_name: str | None
    decided_by_avatar_url: str | None
    decided_at: datetime | None
    executed_at: datetime | None
    created_at: datetime

    @field_validator("arguments")
    @classmethod
    def _redact_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        redacted: dict[str, Any] = redact_json(value)
        return redacted

    @classmethod
    def build(cls, approval: Approval, run: Run, decider: User | None) -> "ApprovalOut":
        return cls(
            id=approval.id,
            run_id=approval.run_id,
            workspace_id=approval.workspace_id,
            run_agent=run.agent,
            run_goal=run.goal,
            tool_name=approval.tool_name,
            policy=approval.policy,
            arguments=approval.arguments,
            status=approval.status,
            decided_by=approval.decided_by,
            decided_by_login=decider.github_login if decider else None,
            decided_by_name=decider.name if decider else None,
            decided_by_avatar_url=decider.avatar_url if decider else None,
            decided_at=approval.decided_at,
            executed_at=approval.executed_at,
            created_at=approval.created_at,
        )


class RunDetail(RunOut):
    steps: list[StepOut]
    approvals: list[ApprovalOut]


class DecideIn(BaseModel):
    decision: Decision


class StreamToken(BaseModel):
    token: str
    expires_at: datetime


class WorkspaceSummary(BaseModel):
    model_config = ConfigDict(
        json_schema_extra=_optional_in_schema(
            "usd_today", "daily_usd_cap", "usd_total", "total_usd_cap"
        )
    )

    runs_today: int
    pending_approvals: int
    # Tokens this workspace's runs used today (UTC).
    workspace_tokens_today: int
    # DAILY_TOKEN_BUDGET is shared by every workspace; this is what counts against it.
    budget_tokens_today: int
    daily_token_budget: int | None
    # Token Factory spend in USD across all workspaces (llm_usage), and its caps.
    usd_today: float = 0.0
    daily_usd_cap: float | None = None
    usd_total: float = 0.0
    total_usd_cap: float | None = None


# --- SSE payloads -----------------------------------------------------------------------


class RunStatusEvent(BaseModel):
    """Sent as `status` whenever the run's state or totals change, and as the final `done`."""

    model_config = ConfigDict(json_schema_extra=_optional_in_schema("ultra_equiv_usd"))

    status: RunStatus
    stop_reason: StopReason | None
    result: str | None
    error: str | None
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None
    ultra_equiv_usd: float | None = None
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def build(cls, run: Run) -> "RunStatusEvent":
        return cls(
            status=run.status,
            stop_reason=run.stop_reason,
            result=run.result,
            error=run.error,
            prompt_tokens=run.prompt_tokens,
            completion_tokens=run.completion_tokens,
            cost_usd=float(run.cost_usd) if run.cost_usd is not None else None,
            ultra_equiv_usd=_float(run.ultra_equiv_usd),
            started_at=run.started_at,
            finished_at=run.finished_at,
        )


class ApprovalStepEvent(BaseModel):
    """Sent as `approval_requested` / `approval_decided`: the step plus the approval."""

    step: StepOut
    approval: ApprovalOut | None


# Event names on the SSE stream, for the web client. The deploy events (DEPLOY_EVENT_NAMES)
# and incident events (MEDIC_EVENT_NAMES) carry a StepOut whose `data` has the typed payload.
SseEventName = Literal[
    "step",
    "approval_requested",
    "approval_decided",
    "status",
    "done",
    "stage_started",
    "stage_finished",
    "build_attempt",
    "verify_result",
    "live_url",
    "rollback_offered",
    "run_finished",
    "incident_opened",
    "evidence_collected",
    "memory_checked",
    "research_done",
    "diagnosis",
    "fix_proposed",
    "fix_applied",
    "incident_resolved",
    "incident_failed",
]
