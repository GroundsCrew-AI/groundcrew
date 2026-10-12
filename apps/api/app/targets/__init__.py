"""Deploy targets (docs/phase4-infra.md, Targets): Kubernetes and AWS ECS behind one protocol.

`target_for` picks the target for a connection: a `k8s` connection, or an `aws` connection
with Tier=deploy (anything else cannot deploy). apply() and rollback() need an ApprovedAction
from app.policy.actions.
"""

from app.config import Settings
from app.connectors.credentials import CredentialsProvider
from app.db.models import Connection
from app.targets.aws_ecs import EcsTarget
from app.targets.base import (
    Deployment,
    DeploySpec,
    EnvValue,
    Health,
    LogLine,
    PlanMismatchError,
    Target,
    TargetError,
    TargetPlan,
)
from app.targets.k8s import K8sTarget

__all__ = [
    "DeploySpec",
    "Deployment",
    "EcsTarget",
    "EnvValue",
    "Health",
    "K8sTarget",
    "LogLine",
    "PlanMismatchError",
    "Target",
    "TargetError",
    "TargetPlan",
    "target_for",
]


def target_for(settings: Settings, credentials: CredentialsProvider, conn: Connection) -> Target:
    if conn.kind == "k8s":
        cluster = str(conn.config.get("cluster") or conn.name)
        return K8sTarget(settings, credentials, conn.id, cluster=cluster)
    if conn.kind == "aws":
        if (conn.config.get("tier") or "observe") != "deploy":
            raise TargetError(f"{conn.name} is connected with Tier=observe, which cannot deploy.")
        return EcsTarget(settings, credentials, conn.id)
    raise TargetError(f"{conn.name} ({conn.kind}) is not a deploy target.")
