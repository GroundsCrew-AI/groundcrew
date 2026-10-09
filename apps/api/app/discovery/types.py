"""Shapes every connector produces and the discovery engine consumes.

A connector (or a deploy target's `discover()`) turns one connection (an AWS account, a
GitHub installation, a Vercel team, a cluster, ...) into `DiscoveredResource`s with stable
`external_id`s, optional `DiscoveredEdge`s it knows for certain, plus facts read from
repositories (`RepoFacts`). The engine upserts the resources, marks the ones it did not see
as stale, and hands everything to app.graph.edges to infer the remaining relationships.

Nothing here may carry a secret value: names, ARNs, and metadata only.
"""

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EdgeKind(StrEnum):
    # Core kinds (roadmap).
    calls = "calls"  # sends requests to (web -> api, ingress -> service)
    reads = "reads"  # reads data from (service -> database)
    deployed_by = "deployed_by"  # is deployed by (service -> workflow, project -> deployment)
    defined_in = "defined_in"  # is defined in (resource -> repository: IaC, image commit)
    # Phase 3 extensions (docs/phase3-interfaces.md): none maps onto a core kind.
    uses = "uses"  # gets a secret or config from (service -> Secrets Manager secret)
    pulls_from = "pulls_from"  # pulls its image from (service -> ECR repository)
    logs_to = "logs_to"  # sends logs to (service -> log group); makes it diagnosable


class Confidence(StrEnum):
    high = "high"
    medium = "medium"
    low = "low"


class Coverage(StrEnum):
    observed = "observed"  # we know it exists and how it is configured
    diagnosable = "diagnosable"  # we can also read its logs or metrics
    actionable = "actionable"  # a target adapter can deploy, roll back, or restart it


class DiscoveredResource(BaseModel):
    provider: str  # aws | azure | gcp | k8s | vercel | vm | github
    # "<service>:<kind>", e.g. ecs:service, rds:db, vercel:project, github:workflow
    type: str
    # Stable across discoveries: ARN, Azure resource ID, "vercel:project:<id>", ...
    external_id: str
    name: str
    account: str | None = None
    region: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    # Non-secret configuration the graph and the UI use (ports, image, endpoints, ...).
    config: dict[str, Any] = Field(default_factory=dict)
    iac_path: str | None = None
    repo: str | None = None


class DiscoveredEdge(BaseModel):
    """A relationship a connector or target knows for certain (from configuration, not
    inferred). Endpoints are external_ids; both must be in the workspace's graph."""

    from_external_id: str
    to_external_id: str
    kind: EdgeKind
    confidence: Confidence = Confidence.high
    evidence: str  # one line a person can check
    rule: str = "declared"


# ----------------------------------------------------------------------------- IaC facts


class TerraformResource(BaseModel):
    """One `resource` block, with names resolved where the expression was simple enough."""

    address: str  # aws_ecs_service.api or aws_ecr_repository.app["api"]
    type: str
    path: str  # file path inside the repository
    line: int | None = None
    # Attributes we could resolve to literal strings (name, family, identifier, ...).
    attributes: dict[str, str] = Field(default_factory=dict)
    # Addresses of other resources this block references (aws_security_group.tasks, ...).
    references: list[str] = Field(default_factory=list)


class WorkflowStep(BaseModel):
    name: str | None = None
    uses: str | None = None
    # `with:` inputs and `env:` values as written (expressions kept, e.g. ${{ vars.X }}).
    inputs: dict[str, str] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)
    run: str | None = None


class WorkflowJob(BaseModel):
    id: str
    name: str | None = None
    needs: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    steps: list[WorkflowStep] = Field(default_factory=list)
    # What the job does, read from its steps.
    role_to_assume: list[str] = Field(default_factory=list)
    pushes_images: list[str] = Field(default_factory=list)  # repo names or expressions
    ecs_deploy: bool = False
    ecs_services: list[str] = Field(default_factory=list)  # names or expressions
    vercel: bool = False  # deploys to or configures Vercel
    vercel_env_keys: list[str] = Field(default_factory=list)  # env vars it sets on Vercel
    # Where a value it sets on Vercel comes from: {"API_URL": "task_ip"} when the step builds
    # it from a running ECS task's IP (ip=$(scripts/api-ip.sh); API_URL="http://$ip:8000" ...).
    vercel_env_sources: dict[str, str] = Field(default_factory=dict)


class Workflow(BaseModel):
    path: str  # .github/workflows/deploy.yml
    name: str
    triggers: list[str] = Field(default_factory=list)
    branches: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    jobs: list[WorkflowJob] = Field(default_factory=list)


class DockerImage(BaseModel):
    path: str  # api/Dockerfile
    base_images: list[str] = Field(default_factory=list)
    exposed_ports: list[int] = Field(default_factory=list)


class ComposeService(BaseModel):
    path: str
    name: str
    image: str | None = None
    build: str | None = None
    ports: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    env_names: list[str] = Field(default_factory=list)


class RepoFacts(BaseModel):
    """What a repository says about the system: IaC, images, and pipelines."""

    repo: str  # owner/name
    html_url: str | None = None
    default_branch: str | None = None
    commit: str | None = None
    terraform: list[TerraformResource] = Field(default_factory=list)
    workflows: list[Workflow] = Field(default_factory=list)
    dockerfiles: list[DockerImage] = Field(default_factory=list)
    compose: list[ComposeService] = Field(default_factory=list)
    # Env var names the code reads (process.env.X, os.environ["X"]), with the file.
    code_env_refs: dict[str, list[str]] = Field(default_factory=dict)
    # Recent commits (sha, message first line, time) and workflow runs, for "recent deploys".
    commits: list[dict[str, Any]] = Field(default_factory=list)
    runs: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ConnectorResult(BaseModel):
    resources: list[DiscoveredResource] = Field(default_factory=list)
    edges: list[DiscoveredEdge] = Field(default_factory=list)
    repos: list[RepoFacts] = Field(default_factory=list)
    # Things a person should know: fallbacks used, permissions missing, limits hit.
    notes: list[str] = Field(default_factory=list)
    truncated: bool = False


# A progress line for the live run timeline.
Progress = Callable[[str], Awaitable[None]]


async def no_progress(_message: str) -> None:
    return None


class ConnectorError(RuntimeError):
    """The connection could not be read. The message never includes a credential."""
