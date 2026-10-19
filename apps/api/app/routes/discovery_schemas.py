"""Request and response models for connections, discovery, and the graph (Phase 3).

Never returned: connection secrets (tokens, kubeconfigs, client secrets), Vercel env values
other than plain *_URL / *_HOST, or repository facts blobs. Resource config goes through
redact_json before it leaves the API.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.connectors.credentials import capabilities
from app.db.models import Connection, Edge, Resource
from app.discovery.engine import FACTS_KEY, PREVIEW_KINDS
from app.graph.roles import role_of
from app.security.redact import redact_json


class ConnectionKind(StrEnum):
    aws = "aws"
    github = "github"
    vercel = "vercel"
    azure = "azure"
    k8s = "k8s"


ConnectionStatus = Literal["pending", "connected", "error"]
# AWS access tier (infra/aws-connect.yaml, parameter Tier): observe = read-only discovery;
# deploy = also the groundcrew-deploy role for deploys into the groundcrew-deploys cluster.
AwsTier = Literal["observe", "deploy"]
AwsCapability = Literal["observe", "deploy", "operate"]


class LastDiscovery(BaseModel):
    at: datetime
    run_id: uuid.UUID | None = None
    status: Literal["succeeded", "failed"]
    error: str | None = None
    duration_ms: int | None = None
    resources: int | None = None
    stale: int | None = None
    truncated: bool = False
    notes: list[str] = Field(default_factory=list)


class ConnectionOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    kind: ConnectionKind
    name: str
    status: ConnectionStatus
    # Azure: stored, but discovery runs on fixtures only for now.
    preview: bool
    # AWS account ID, GitHub owner, Vercel team, Azure subscription, or k8s cluster.
    account: str | None = None
    role_arn: str | None = None
    external_id: str | None = None
    repos: list[str] = Field(default_factory=list)
    team_id: str | None = None
    resource_group: str | None = None
    server: str | None = None
    # AWS: the stack's Tier, and the deploy role once verified (Tier=deploy).
    tier: AwsTier | None = None
    deploy_role_arn: str | None = None
    # AWS (Phase 5): observe, deploy (verified deploy role), operate (verified operate role:
    # approved emergency fixes). Kubernetes: observe, operate (rights checked, turned on).
    # Null for other kinds.
    capabilities: list[AwsCapability] | None = None
    operate_role_arn: str | None = None
    # AWS: the region the connect stack was created in (CloudFormation console links).
    region: str | None = None
    # Kubernetes: namespaces the kubeconfig could list when it was verified.
    namespaces: int | None = None
    error: str | None = None
    resource_count: int = 0
    last_discovery: LastDiscovery | None = None
    created_at: datetime

    @classmethod
    def build(cls, conn: Connection, resource_count: int = 0) -> "ConnectionOut":
        c = conn.config or {}
        last = LastDiscovery.model_validate(conn.last_discovery) if conn.last_discovery else None
        return cls(
            id=conn.id,
            workspace_id=conn.workspace_id,
            kind=ConnectionKind(conn.kind),
            name=conn.name,
            status=conn.status if conn.status in ("pending", "connected", "error") else "error",
            preview=conn.kind in PREVIEW_KINDS,
            account=c.get("account_id")
            or c.get("account")
            or c.get("team_slug")
            or c.get("subscription_id")
            or c.get("cluster"),
            role_arn=c.get("role_arn"),
            external_id=c.get("external_id"),
            repos=list(c.get("repos") or []),
            team_id=c.get("team_id"),
            resource_group=c.get("resource_group"),
            server=c.get("server"),
            tier=(c.get("tier") or "observe") if conn.kind == "aws" else None,
            deploy_role_arn=c.get("deploy_role_arn"),
            capabilities=capabilities(conn) if conn.kind in ("aws", "k8s") else None,
            operate_role_arn=c.get("operate_role_arn"),
            region=c.get("region") if conn.kind == "aws" else None,
            namespaces=c.get("namespaces"),
            error=c.get("last_error"),
            resource_count=resource_count,
            last_discovery=last,
            created_at=conn.created_at,
        )


class AwsStartIn(BaseModel):
    workspace_id: uuid.UUID
    tier: AwsTier = "observe"


class AwsStartOut(BaseModel):
    connection_id: uuid.UUID
    tier: AwsTier
    external_id: str
    principal_arn: str
    region: str
    stack_name: str
    # One-click CloudFormation quick-create link. Null when AWS_CONNECT_TEMPLATE_URL (an S3
    # URL, which CloudFormation requires) is not set; use cli_command then.
    console_url: str | None
    # GET this API path for the template body (infra/aws-connect.yaml).
    template_path: str
    cli_command: str
    role_arn_command: str


class AwsCompleteIn(BaseModel):
    connection_id: uuid.UUID
    role_arn: str = Field(min_length=20, max_length=2048)


class AwsTemplateUrlOut(BaseModel):
    # S3 URL of infra/aws-connect.yaml (AWS_CONNECT_TEMPLATE_URL), for CloudFormation's
    # "Amazon S3 URL" field when updating a stack. Null when not configured: download the
    # template from template_path and upload it instead.
    url: str | None
    template_path: str


class AwsTierIn(BaseModel):
    tier: AwsTier


class AwsOperateIn(BaseModel):
    """Turn approved emergency fixes on (after the stack has EnableOperate=true) or off."""

    enabled: bool


class K8sOperateIn(BaseModel):
    """Turn approved emergency fixes (rollout undo, restart) on or off for a cluster."""

    enabled: bool


class GitHubRepoOut(BaseModel):
    full_name: str
    private: bool
    default_branch: str | None
    linked: bool


class GitHubInstallationOut(BaseModel):
    id: int
    account: str
    repos: list[GitHubRepoOut]


class GitHubInstallationsOut(BaseModel):
    configured: bool
    install_url: str | None
    installations: list[GitHubInstallationOut]
    error: str | None = None


class GitHubLinkIn(BaseModel):
    workspace_id: uuid.UUID
    installation_id: int
    repos: list[str] = Field(min_length=1, max_length=50)


class VercelConnectIn(BaseModel):
    workspace_id: uuid.UUID
    token: str = Field(min_length=8, max_length=500)
    team_id: str | None = Field(default=None, max_length=100)


class AzureConnectIn(BaseModel):
    workspace_id: uuid.UUID
    tenant_id: str = Field(min_length=1, max_length=100)
    client_id: str = Field(min_length=1, max_length=100)
    client_secret: str = Field(min_length=1, max_length=500)
    subscription_id: str = Field(min_length=1, max_length=100)
    resource_group: str = Field(min_length=1, max_length=90)


class KubernetesConnectIn(BaseModel):
    workspace_id: uuid.UUID
    name: str | None = Field(default=None, max_length=200)
    kubeconfig: str = Field(min_length=20, max_length=200_000)


class ConnectionDeleted(BaseModel):
    id: uuid.UUID
    removed_resources: int
    # What the customer still has to do on their side to remove our access.
    cleanup: list[str]


# ------------------------------------------------------------------------------- graph


Coverage = Literal["observed", "diagnosable", "actionable"]
EdgeConfidence = Literal["high", "medium", "low"]


def _facts(r: Resource) -> list[str]:
    """Two or three short facts for the node card."""
    c = r.config or {}
    out: list[str] = []
    if r.type == "ecs:service":
        out.append(f"{c.get('running_count', 0)}/{c.get('desired_count', 0)} running")
        if c.get("public_ips"):
            out.append(f"IP {c['public_ips'][0]}")
        tags = sorted(
            {str(x.get("image_tag")) for x in c.get("containers") or [] if x.get("image_tag")}
        )
        if tags:
            out.append(f"image {tags[0][:7]}")
    elif r.type == "rds:db":
        out.append(f"{c.get('engine')} {c.get('engine_version')}")
        out.append("public" if c.get("publicly_accessible") else "private")
    elif r.type == "ecr:repository":
        images = c.get("images") or []
        if images:
            tags = images[0].get("tags") or []
            out.append(f"latest {str(tags[0])[:7]}" if tags else "untagged")
    elif r.type == "secretsmanager:secret":
        if c.get("last_changed_at"):
            out.append(f"changed {str(c['last_changed_at'])[:10]}")
    elif r.type == "vercel:project":
        latest = c.get("latest_production") or {}
        if latest.get("state"):
            out.append(str(latest["state"]).lower())
        aliases = c.get("production_aliases") or []
        if aliases:
            out.append(str(aliases[0]))
    elif r.type == "github:workflow":
        last = c.get("last_run") or {}
        if last:
            out.append(f"last run {last.get('conclusion') or last.get('status')}")
    elif r.type == "logs:log-group":
        if c.get("retention_days"):
            out.append(f"{c['retention_days']} day retention")
    elif r.type == "ec2:security-group" and c.get("open_to_world"):
        out.append("open to 0.0.0.0/0: " + ", ".join(c["open_to_world"]))
    return out[:3]


def _iac_url(r: Resource) -> str | None:
    if not r.repo or not r.iac_path:
        return None
    path, _, line = r.iac_path.partition("#")
    return f"https://github.com/{r.repo}/blob/HEAD/{path}" + (f"#{line}" if line else "")


class ResourceOut(BaseModel):
    id: uuid.UUID
    connection_id: uuid.UUID | None
    provider: str
    type: str
    name: str
    account: str | None
    region: str | None
    coverage: Coverage
    # app: part of the system; tooling: manages it (state buckets, Groundcrew's role, ...)
    role: Literal["app", "tooling"]
    stale: bool
    iac_path: str | None
    iac_url: str | None
    repo: str | None
    facts: list[str]
    last_seen_at: datetime | None

    @classmethod
    def build(cls, r: Resource) -> "ResourceOut":
        return cls(
            id=r.id,
            connection_id=r.connection_id,
            provider=r.provider,
            type=r.type,
            name=r.name,
            account=r.account,
            region=r.region,
            coverage=r.coverage
            if r.coverage in ("observed", "diagnosable", "actionable")
            else "observed",
            role=role_of(r.type, r.name, r.tags),
            stale=r.stale,
            iac_path=r.iac_path,
            iac_url=_iac_url(r),
            repo=r.repo,
            facts=_facts(r),
            last_seen_at=r.last_seen_at,
        )


class EdgeOut(BaseModel):
    id: uuid.UUID
    from_id: uuid.UUID
    to_id: uuid.UUID
    kind: str
    confidence: EdgeConfidence
    evidence: str
    rule: str | None

    @classmethod
    def build(cls, e: Edge) -> "EdgeOut":
        ev = e.evidence or {}
        conf = e.confidence if e.confidence in ("high", "medium", "low") else "medium"
        return cls(
            id=e.id,
            from_id=e.from_id,
            to_id=e.to_id,
            kind=e.kind,
            confidence=conf,
            evidence=str(ev.get("text") or ""),
            rule=ev.get("rule"),
        )


class SystemSummaryOut(BaseModel):
    text: str | None
    risks: list[str]
    generated_at: datetime | None
    run_id: uuid.UUID | None
    error: str | None = None


class GraphOut(BaseModel):
    nodes: list[ResourceOut]
    edges: list[EdgeOut]
    summary: SystemSummaryOut | None
    # The latest discovery run of this workspace, for "Last discovery" and the live link.
    last_run_id: uuid.UUID | None
    last_run_status: str | None
    last_run_at: datetime | None
    stale_count: int


class Neighbor(BaseModel):
    id: uuid.UUID
    name: str
    type: str
    provider: str


class ResourceEdgeOut(EdgeOut):
    direction: Literal["out", "in"]
    other: Neighbor


class DeployOut(BaseModel):
    at: str | None
    source: str
    status: str | None
    title: str | None
    ref: str | None
    url: str | None


class ResourceDetail(ResourceOut):
    tags: dict[str, str]
    config: dict[str, Any]
    edges: list[ResourceEdgeOut]
    recent_deploys: list[DeployOut]

    @staticmethod
    def safe_config(r: Resource) -> dict[str, Any]:
        config = {k: v for k, v in (r.config or {}).items() if k != FACTS_KEY}
        cleaned: dict[str, Any] = redact_json(config)
        return cleaned
