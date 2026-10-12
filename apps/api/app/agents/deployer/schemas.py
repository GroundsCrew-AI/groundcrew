"""What Deployer asks the model for (plan_deploy, Super with reasoning low)."""

from typing import Literal

from pydantic import BaseModel, Field

# Task size. Targets run "small" (k8s 250m / 512Mi, Fargate 0.25 vCPU / 0.5 GB); other sizes
# are recorded for the plan but not applied yet. Every choice field allows "other".
Size = Literal["small", "medium", "large", "other"]


class DeployDraft(BaseModel):
    replicas: int = Field(ge=1, le=3, description="How many copies to run (1 for a demo).")
    size: Size = Field(description="How much CPU and memory the app needs.")
    secret_env: list[str] = Field(
        default_factory=list,
        description="Names of env vars whose values are secrets (passwords, keys, tokens, "
        "connection strings).",
    )
    rationale: str = Field(description="One short paragraph: why these choices.")
