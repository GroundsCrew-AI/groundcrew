"""The shape of `runs.state` for deploy runs (agent="deploy"). docs/deploys-contract.md.

`stages` lets the pipeline pause at an approval and resume in a new job. `deploy` is written
once, when the run reaches a final deploy status, and is what Medic (Phase 5) reads for recent
deploys and rollback:

    from app.pipeline.state import DeployRecord, PipelineState
    record = PipelineState.model_validate(run.state).deploy

Secret env values never appear here, in approvals, steps, or logs: they live Fernet-encrypted
in the job payload only and are purged when the run ends.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.pipeline.events import DeployStage, StageStatus, TargetKind

DeployStatus = Literal["live", "failed", "rolled_back"]


class DeployRecord(BaseModel):
    """runs.state["deploy"]: one finished deployment."""

    target: TargetKind
    connection_id: uuid.UUID
    app: str
    # Fully qualified, pushed image, for example kind-registry:5000/legacy-flask-app:<sha12>.
    image: str
    commit: str | None
    # owner/name for GitHub sources, fixture:<name> for fixtures.
    repo: str
    plan_summary: str
    live_url: str | None
    # Target-specific revision: Deployment revision (k8s) or task definition ARN (ECS).
    revision: str | None
    # What rollback() goes back to. Null on a first deploy.
    previous_revision: str | None
    started_at: datetime
    finished_at: datetime
    status: DeployStatus


class StageState(BaseModel):
    status: Literal["pending", "running", "waiting_approval"] | StageStatus = "pending"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # Pending approval for this stage (approval or rollback), so a resumed job finds it.
    approval_id: uuid.UUID | None = None


class PipelineState(BaseModel):
    """runs.state for agent="deploy". Unknown keys are kept for forward compatibility."""

    model_config = ConfigDict(extra="allow")

    deploy_id: uuid.UUID | None = None
    stages: dict[DeployStage, StageState] = Field(default_factory=dict)
    deploy: DeployRecord | None = None
