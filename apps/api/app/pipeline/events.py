"""Typed payloads for deploy run events (docs/events.md).

Each deploy event is a run step: `run_steps.kind` is the event name and `run_steps.data` holds
one of the payloads below, discriminated by `type` (always equal to the step kind). The SSE
stream sends the step under the same event name, so a reconnect with Last-Event-ID resumes
deploy events exactly like any other step, and GET /runs/{id} returns them on reload.

Approvals reuse the existing `approval_requested` / `approval_decided` events; for a deploy
the requested step carries a `deploy_plan` payload (the exact rendered plan, secrets masked).

Free text here (log tails, error summaries, diffs) is redacted before it is stored
(app.steps.record_step) and capped by the producer: log tails at LOG_TAIL_CHARS.
"""

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.db.models import RunStatus, StopReason

LOG_TAIL_CHARS = 8000
MAX_BUILD_ATTEMPTS = 5

# Pipeline stages in order. Builder owns build, smoke, and push; Deployer the rest.
DeployStage = Literal[
    "scout",
    "build",
    "smoke",
    "push",
    "plan",
    "preflight",
    "approval",
    "apply",
    "verify",
    "rollback",
]
StageAgent = Literal["scout", "builder", "deployer"]
StageStatus = Literal["succeeded", "failed", "skipped"]
TargetKind = Literal["k8s", "aws_ecs"]

# Event names a deploy run adds to the run stream (besides step, approval_requested,
# approval_decided, status, done).
DEPLOY_EVENT_NAMES = (
    "stage_started",
    "stage_finished",
    "build_attempt",
    "verify_result",
    "live_url",
    "rollback_offered",
    "run_finished",
)


class StageStarted(BaseModel):
    type: Literal["stage_started"] = "stage_started"
    stage: DeployStage
    agent: StageAgent


class StageFinished(BaseModel):
    type: Literal["stage_finished"] = "stage_finished"
    stage: DeployStage
    agent: StageAgent
    status: StageStatus
    duration_ms: int
    # One line for the timeline, for example "Flask on port 8000, health /health".
    summary: str | None = None


class AttemptModelCall(BaseModel):
    """The model call that wrote this attempt's Dockerfile. Its own `llm` step is in the
    timeline too; this copy lets the attempt card show the badge without a lookup."""

    task: Literal["write_dockerfile", "fix_build"]
    model_tier: str
    model_id: str
    reasoning_mode: str
    escalated: bool
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None
    step_seq: int


class BuildAttempt(BaseModel):
    type: Literal["build_attempt"] = "build_attempt"
    attempt: int = Field(ge=1, le=MAX_BUILD_ATTEMPTS)
    max_attempts: int = MAX_BUILD_ATTEMPTS
    # repo: the repository's own Dockerfile, built as is. generated: written by
    # write_dockerfile. fixed: patched by fix_build after the previous attempt failed.
    dockerfile_source: Literal["repo", "generated", "fixed"]
    # Unified diff against the previous attempt's Dockerfile; the full file on attempt 1.
    dockerfile_diff: str
    rationale: str | None = None
    ok: bool
    duration_ms: int
    # On failure: the Dockerfile step that failed and a one-line cause, for the collapsed card.
    failing_step: str | None = None
    error_summary: str | None = None
    # Last LOG_TAIL_CHARS of the build log, redacted.
    log_tail: str | None = None
    # On success: the local image reference in the sandbox (pushed later in the push stage).
    image_ref: str | None = None
    model_call: AttemptModelCall | None = None


class EnvVarView(BaseModel):
    """An env var as shown on the approval card and in the deploy detail. A secret's value is
    never shown; `value` is null for secrets and for missing values."""

    name: str
    secret: bool
    required: bool
    provided: bool
    value: str | None = None


class PlannedResource(BaseModel):
    kind: str = Field(examples=["Deployment", "ecs:Service"])
    name: str
    action: Literal["create", "update", "unchanged", "delete"]
    detail: str | None = None


class PreflightCheck(BaseModel):
    name: Literal["image", "env", "port", "name_collision", "quota", "other"]
    status: Literal["pass", "warn", "fail"]
    detail: str
    # A failed check blocks the apply unless this says it was overridden (allow_missing_env).
    overridden: bool = False


class DeployPlan(BaseModel):
    """What the approval card shows: the exact plan that runs after approval."""

    type: Literal["deploy_plan"] = "deploy_plan"
    target: TargetKind
    target_connection_id: uuid.UUID
    app: str
    image: str
    port: int
    health_path: str | None
    replicas: int
    cpu: str = Field(examples=["250m", "256"])
    memory: str = Field(examples=["512Mi", "512"])
    env: list[EnvVarView]
    resources: list[PlannedResource]
    preflight: list[PreflightCheck]
    allow_missing_env: bool
    # Where the app will answer once live, when known before apply (kind ingress host).
    expected_url: str | None = None
    # The rendered manifests (k8s) or task definition and service settings (ECS), with every
    # secret value replaced by "********". This text is what the approval approves.
    rendered: str
    rendered_format: Literal["yaml", "json"]
    # Set when this is a rollback offered after a failed verify (an emergency action).
    rollback_to_revision: str | None = None


class VerifyCheck(BaseModel):
    name: Literal["rollout", "health", "root", "other"]
    ok: bool
    detail: str
    url: str | None = None
    status_code: int | None = None
    latency_ms: int | None = None


class VerifyResult(BaseModel):
    type: Literal["verify_result"] = "verify_result"
    ok: bool
    checks: list[VerifyCheck]
    duration_ms: int
    # On failure: the last lines of the app's logs (redacted), so the rollback card can show why.
    logs_tail: str | None = None


class LiveUrl(BaseModel):
    type: Literal["live_url"] = "live_url"
    url: str
    target: TargetKind
    app: str
    # Graph resource registered for this deployment (ResourceSink), for "View on Map".
    resource_id: uuid.UUID | None = None


class RollbackOffered(BaseModel):
    type: Literal["rollback_offered"] = "rollback_offered"
    # The emergency approval to decide; its approval_requested step follows.
    approval_id: uuid.UUID
    reason: str
    from_revision: str
    # Null on a first deploy: rollback then removes the new deployment instead.
    to_revision: str | None


class RunFinished(BaseModel):
    """Last event of a deploy run, before the generic `done`."""

    type: Literal["run_finished"] = "run_finished"
    status: RunStatus
    stop_reason: StopReason | None
    error: str | None = None
    live_url: str | None = None
    build_attempts: int
    # True when a build passed after Builder changed the Dockerfile because of a failure.
    automatic_fix: bool
    duration_ms: int | None
    cost_usd: float | None
    ultra_equiv_usd: float | None = None


DeployEventData = Annotated[
    StageStarted
    | StageFinished
    | BuildAttempt
    | DeployPlan
    | VerifyResult
    | LiveUrl
    | RollbackOffered
    | RunFinished,
    Field(discriminator="type"),
]
