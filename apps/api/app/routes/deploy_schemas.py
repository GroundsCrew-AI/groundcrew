"""Request and response models for /deploys (docs/deploys-contract.md).

Env values come in as SecretStr and never go back out: responses show names, whether each is
secret, and whether a value was given. Plain (non-secret) values appear only in the rendered
plan on the approval card.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.db.models import RunStatus, StopReason
from app.pipeline.events import DeployPlan, DeployStage, EnvVarView, TargetKind
from app.pipeline.state import DeployRecord, StageState

MAX_ENV_VARS = 100
MAX_ENV_VALUE_CHARS = 16_384
# A service folder inside the repository: relative, no "..", "." for the root.
SERVICE_PATH = r"^(\.|[A-Za-z0-9_][A-Za-z0-9._-]*(/[A-Za-z0-9_][A-Za-z0-9._-]*){0,5})$"

# --- request ------------------------------------------------------------------------------


class GitHubRepoSource(BaseModel):
    """A repository the workspace's GitHub connection can read."""

    model_config = ConfigDict(extra="forbid")

    connection_id: uuid.UUID
    repo: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$",
        examples=["GroundsCrew-AI/legacy-flask-app"],
    )
    # Branch, tag, or commit SHA. The commit actually built is in the deploy record.
    ref: str = Field(min_length=1, max_length=255, pattern=r"^[^\s~^:?*\[\\]+$", examples=["main"])
    # The service to deploy when the repo holds several (GET /deploys/services lists them).
    # Scout reads and Builder builds only this folder. Omitted: the root, or the one service.
    path: str | None = Field(default=None, max_length=255, pattern=SERVICE_PATH, examples=["api"])


class FixtureSource(BaseModel):
    """A repo under demo/fixtures/ (tests and local dev; refused when APP_ENV is not dev)."""

    model_config = ConfigDict(extra="forbid")

    fixture: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$", examples=["py-build-trap"])
    path: str | None = Field(default=None, max_length=255, pattern=SERVICE_PATH)


DeploySource = GitHubRepoSource | FixtureSource


class EnvVarIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$", examples=["DATABASE_URL"])
    value: SecretStr = Field(max_length=MAX_ENV_VALUE_CHARS)
    # Secret values go to a Kubernetes Secret / Secrets Manager and are masked everywhere.
    secret: bool = False


def _unique(env: list[EnvVarIn]) -> list[EnvVarIn]:
    seen: set[str] = set()
    for var in env:
        if var.name in seen:
            raise ValueError(f"env var {var.name} is listed twice")
        seen.add(var.name)
    return env


class DeployOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Deploy even if Scout marked an env var required and no value was given. The approval
    # card lists the missing names.
    allow_missing_env: bool = False


class DeployCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: uuid.UUID
    repo: DeploySource
    # A k8s connection, an AWS connection with Tier=deploy, or a Groundcrew agent.
    target_connection_id: uuid.UUID
    # Agent targets: the service on the server to update (its `allow` lists deploy).
    target_service: str | None = Field(default=None, max_length=100)
    env: list[EnvVarIn] = Field(default_factory=list, max_length=MAX_ENV_VARS)
    options: DeployOptions = Field(default_factory=DeployOptions)

    @field_validator("env")
    @classmethod
    def _unique_names(cls, env: list[EnvVarIn]) -> list[EnvVarIn]:
        return _unique(env)


class DeployEnvUpdate(BaseModel):
    """POST /deploys/{id}/env: fix a failed env preflight at the approval card. The pending
    plan approval is replaced; only preflight runs again, with the same built image."""

    model_config = ConfigDict(extra="forbid")

    # The deploy's pending plan approval (409 when it is not).
    approval_id: uuid.UUID
    # Merged by name into the deploy's env. Same rules as POST /deploys.
    env: list[EnvVarIn] = Field(default_factory=list, max_length=MAX_ENV_VARS)
    # "Continue without it": the env check stays failed but overridden.
    allow_missing_env: bool = False

    @field_validator("env")
    @classmethod
    def _unique_names(cls, env: list[EnvVarIn]) -> list[EnvVarIn]:
        return _unique(env)


class DeployEnvUpdated(BaseModel):
    # The approval that was replaced; a new approval_requested follows on the run stream.
    approval_id: uuid.UUID


class RepoService(BaseModel):
    # "." is the repository root.
    path: str
    # The manifests that make it a service (package.json, go.mod, ...).
    manifests: list[str]


class RepoServices(BaseModel):
    """GET /deploys/services: the folders of a repository that can be deployed on their own."""

    services: list[RepoService]
    # More than one service and no root app: the deploy form asks which one.
    choose: bool


class DeployCreated(BaseModel):
    deploy_id: uuid.UUID
    # Open GET /runs/{run_id}/events (after POST /runs/{run_id}/stream-token) for the timeline.
    run_id: uuid.UUID


# --- responses ----------------------------------------------------------------------------

# queued until the worker starts; running through the stages; waiting_approval at the plan or
# a rollback; then live, failed, rolled_back, or stopped (by a user or a budget).
DeployState = Literal[
    "queued", "running", "waiting_approval", "live", "failed", "rolled_back", "stopped"
]


class DeploySourceOut(BaseModel):
    kind: Literal["github", "fixture"]
    # owner/name for GitHub, the fixture name otherwise.
    repo: str
    ref: str | None
    # The service folder deployed; null for the repository root.
    path: str | None = None
    connection_id: uuid.UUID | None
    # The commit that was built, once Scout has checked it out.
    commit: str | None


class DeployTargetOut(BaseModel):
    connection_id: uuid.UUID
    kind: TargetKind
    # The connection's name: cluster or "AWS 881050425883".
    name: str


class DeploySummary(BaseModel):
    """One row of the deploys list."""

    id: uuid.UUID
    run_id: uuid.UUID
    workspace_id: uuid.UUID
    status: DeployState
    run_status: RunStatus
    stop_reason: StopReason | None
    # The stage running now, or the last one reached.
    stage: DeployStage | None
    app: str | None
    source: DeploySourceOut
    target: DeployTargetOut
    live_url: str | None
    build_attempts: int
    automatic_fix: bool
    cost_usd: float | None
    ultra_equiv_usd: float | None
    error: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None


class DeployPage(BaseModel):
    items: list[DeploySummary]
    # Pass as ?cursor= to get the next page. Null when there are no more deploys.
    next_cursor: str | None


class StageOut(StageState):
    stage: DeployStage


class DeployDetail(DeploySummary):
    # Names and flags only; never values.
    env: list[EnvVarView]
    allow_missing_env: bool
    stages: list[StageOut]
    # The plan shown on the approval card, once rendered.
    plan: DeployPlan | None
    # runs.state["deploy"], once the run reached live, failed, or rolled_back.
    deploy: DeployRecord | None
