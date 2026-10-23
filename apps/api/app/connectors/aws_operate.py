"""The groundcrew-operate role's scope, from what discovery found.

`suggested_values` turns a connection's discovered resources into the stack parameters an owner
pastes when turning emergency fixes on: one wildcard per ECS cluster, the services' execution
and task roles, the Lambda functions, the app's secrets (by name, any version suffix), and its
EC2 instances. Groundcrew's own resources (the platform host, its deploy cluster, its roles and
secrets) and anything marked tooling are left out, so the role cannot act on them.

`unreachable` asks AWS, with the operate role and read calls only, which discovered ECS
services and Lambda functions the role may not touch, so Verify can refuse a stack whose lists
still name nothing real (the template's placeholders).
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.connectors.aws import DENIED_CODES, error_code
from app.db.models import Resource
from app.graph.roles import role_of

TYPES = ("ecs:service", "lambda:function", "secretsmanager:secret", "ec2:instance")


@dataclass
class OperateValues:
    services: list[str] = field(default_factory=list)
    pass_roles: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    instances: list[str] = field(default_factory=list)


def _ours(resource: Resource) -> bool:
    """Groundcrew's own: the platform host, the cluster it deploys into, its secrets."""
    name = resource.name.lower()
    cluster = str((resource.config or {}).get("cluster_arn") or "").rsplit("/", 1)[-1]
    return name.startswith("groundcrew") or cluster.startswith("groundcrew")


def _app(resources: Iterable[Resource]) -> list[Resource]:
    return [
        r
        for r in resources
        if not r.stale
        and r.type in TYPES
        and role_of(r.type, r.name, r.tags or {}) == "app"
        and not _ours(r)
    ]


def _add(values: list[str], value: str | None) -> None:
    if value and value not in values:
        values.append(value)


def suggested_values(resources: Iterable[Resource]) -> OperateValues:
    out = OperateValues()
    for r in _app(resources):
        config: dict[str, Any] = r.config or {}
        if r.type == "ecs:service":
            cluster = str(config.get("cluster_arn") or "")
            if ":cluster/" in cluster:
                _add(out.services, cluster.replace(":cluster/", ":service/", 1) + "/*")
            for key in ("execution_role_arn", "task_role_arn"):
                role = str(config.get(key) or "")
                if role and not role.rsplit("/", 1)[-1].startswith("groundcrew-"):
                    _add(out.pass_roles, role)
        elif r.type == "lambda:function":
            _add(out.functions, r.external_id)
        elif r.type == "secretsmanager:secret" and ":secret:" in r.external_id:
            # The six-character suffix changes when a secret is created again.
            _add(out.secrets, r.external_id.rsplit("-", 1)[0] + "-*")
        elif r.type == "ec2:instance" and ":instance/" in r.external_id:
            _add(out.instances, r.external_id)
    return out


def probe_targets(resources: Iterable[Resource]) -> list[Resource]:
    """The app's ECS services and Lambda functions: what Verify checks the role can reach."""
    return [r for r in _app(resources) if r.type in ("ecs:service", "lambda:function")]


def unreachable(session: Any, targets: list[Resource]) -> tuple[list[str], list[str]]:
    """(reachable, refused) display names, from one read call per target with the operate
    role. A call that fails for another reason counts as reachable: AWS decides at run time."""
    reachable: list[str] = []
    refused: list[str] = []
    clients: dict[tuple[str, str], Any] = {}

    def client(service: str, region: str | None) -> Any:
        key = (service, region or "")
        if key not in clients:
            clients[key] = session.client(service, region_name=region)
        return clients[key]

    for r in targets:
        config: dict[str, Any] = r.config or {}
        try:
            if r.type == "ecs:service":
                cluster = str(config.get("cluster_arn") or "")
                label = f"{cluster.rsplit('/', 1)[-1]}/{r.name}"
                client("ecs", r.region).describe_services(cluster=cluster, services=[r.name])
            else:
                label = r.name
                client("lambda", r.region).list_versions_by_function(
                    FunctionName=r.external_id, MaxItems=1
                )
        except ClientError as exc:
            (refused if error_code(exc) in DENIED_CODES else reachable).append(label)
            continue
        except BotoCoreError:
            reachable.append(label)
            continue
        reachable.append(label)
    return reachable, refused
