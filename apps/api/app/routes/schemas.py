"""Request and response models for the runs and approvals API.

runs.messages (the raw conversation) is never returned: it can hold full prompts.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import ApprovalStatus, RunStatus, StopReason, ToolPolicy
from app.policy.engine import Decision


class RunCreate(BaseModel):
    agent: str = Field(examples=["echo_ops"])
    goal: str = Field(min_length=1, max_length=4000)
    workspace_id: uuid.UUID


class RunCreated(BaseModel):
    id: uuid.UUID
    status: RunStatus


class StepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    seq: int
    agent: str
    kind: str
    model_tier: str | None
    model_id: str | None
    tool_name: str | None
    input_summary: str | None
    output_summary: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int | None
    cost_usd: float | None
    created_at: datetime


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
    cost_usd: float | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunWithSteps(RunOut):
    steps: list[StepOut]


class RunStatusEvent(BaseModel):
    status: RunStatus
    stop_reason: StopReason | None
    result: str | None
    error: str | None


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    workspace_id: uuid.UUID
    tool_name: str
    policy: ToolPolicy
    arguments: dict[str, Any]
    status: ApprovalStatus
    decided_by: str | None
    decided_at: datetime | None
    executed_at: datetime | None
    created_at: datetime


class DecideIn(BaseModel):
    decision: Decision
