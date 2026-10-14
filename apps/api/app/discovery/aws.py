"""AWS discovery: inventory with Resource Explorer, details with a few core describes.

Per region in DISCOVERY_REGIONS:
1. Resource Explorer search through the default view (the index the connect template made).
   If there is no index or view, fall back to the tagging API (GetResources).
2. Core describes, which carry the details the graph needs: ECS clusters, services, task
   definitions, and running tasks with their ENI public IPs; ECR; RDS; VPCs, subnets, and
   security groups; Lambda; log groups; secret metadata.
3. Cloud Control GetResource for a few other inventory types (S3 buckets, tables, queues,
   ...), capped.
Globally: the IAM roles tasks use, and roles GitHub Actions can assume through OIDC.

All calls go through AwsReader (read-only allowlist, allowed regions only). Secret values are
never requested; environment variable values are dropped except non-secret endpoint-like
names (see `keep_env_value`).
"""

import asyncio
import functools
import json
import logging
import re
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any
from urllib.parse import unquote

from app.connectors.aws import AwsReader, error_code, error_text
from app.discovery.types import ConnectorResult, DiscoveredResource, Progress, no_progress
from app.security.redact import redact, redact_json

logger = logging.getLogger("groundcrew.discovery.aws")

PROVIDER = "aws"

# Inventory types we show even without a dedicated describe. Others (ENIs, route tables, task
# definition revisions, log streams, ...) are noise on a map and are skipped.
EXTRA_TYPES = frozenset(
    {
        "s3:bucket",
        "dynamodb:table",
        "sqs:queue",
        "sns:topic",
        "elasticloadbalancing:loadbalancer/app",
        "elasticloadbalancing:loadbalancer/net",
        "elasticloadbalancing:loadbalancer",
        "ec2:instance",
        "rds:cluster",
        "elasticache:cluster",
        "apigateway:restapis",
        "cloudfront:distribution",
        "events:rule",
        "states:stateMachine",
    }
)

# Per-resource tag lookups (Lambda ListTags) per region, at most.
MAX_TAG_CALLS = 100

# Inventory type -> (Cloud Control type name, how to get its identifier from the ARN).
CLOUD_CONTROL_TYPES: dict[str, tuple[str, Callable[[str], str]]] = {
    "s3:bucket": ("AWS::S3::Bucket", lambda arn: arn.rsplit(":", 1)[-1]),
    "dynamodb:table": ("AWS::DynamoDB::Table", lambda arn: arn.rsplit("/", 1)[-1]),
    "sns:topic": ("AWS::SNS::Topic", lambda arn: arn),
    "elasticloadbalancing:loadbalancer/app": (
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        lambda arn: arn,
    ),
    "ec2:instance": ("AWS::EC2::Instance", lambda arn: arn.rsplit("/", 1)[-1]),
}
MAX_CLOUD_CONTROL_CALLS = 25

# Env var names whose values are endpoints, not secrets. Every other value is dropped.
_ENDPOINT_SUFFIXES = ("_URL", "_HOST", "_ENDPOINT", "_PORT", "_ADDR", "_ADDRESS")
_SECRETISH = re.compile(r"(?i)password|passwd|secret|token|key|credential|auth|dsn|database_url")


def keep_env_value(name: str) -> bool:
    """True when an env var's value is an endpoint we may read (API_URL, REDIS_HOST, ...).
    Names that hint at a credential never qualify, even with an endpoint suffix."""
    upper = name.upper()
    return upper.endswith(_ENDPOINT_SUFFIXES) and not _SECRETISH.search(name)


def _strip_userinfo(value: str) -> str:
    """Drop user:password@ from a URL-like value, just in case."""
    return re.sub(r"(//)[^/@\s]+@", r"\1", value)


def env_summary(env: Iterable[dict[str, Any]]) -> tuple[list[str], dict[str, str]]:
    """Names of all env vars, and values only for endpoint-like names."""
    names: list[str] = []
    values: dict[str, str] = {}
    for item in env:
        name = str(item.get("name") or item.get("Name") or "")
        if not name:
            continue
        names.append(name)
        value = item.get("value", item.get("Value"))
        if value is not None and keep_env_value(name):
            values[name] = _strip_userinfo(str(value))
    return sorted(names), values


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _tags(items: Iterable[dict[str, Any]] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for tag in items or []:
        key = tag.get("Key", tag.get("key"))
        if key is not None:
            out[str(key)] = str(tag.get("Value", tag.get("value", "")))
    return out


def parse_image(image: str) -> dict[str, str | None]:
    """Split an image URI into registry, repository, tag, and digest."""
    digest = None
    if "@" in image:
        image, digest = image.split("@", 1)
    tag = None
    last = image.rsplit("/", 1)[-1]
    if ":" in last:
        image, tag = image.rsplit(":", 1)
    registry, _, repository = image.partition("/") if "/" in image else ("", "", image)
    return {"registry": registry or None, "repository": repository, "tag": tag, "digest": digest}


def is_commit_sha(tag: str | None) -> bool:
    return bool(tag and re.fullmatch(r"[0-9a-f]{7,40}", tag))


def policy_document(doc: Any) -> dict[str, Any]:
    """IAM policy documents come URL-encoded from the API; boto3 usually decodes them."""
    if isinstance(doc, dict):
        return doc
    if isinstance(doc, str):
        try:
            parsed = json.loads(unquote(doc))
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def summarize_policy(doc: dict[str, Any], max_statements: int = 20) -> list[dict[str, Any]]:
    """Statements as {effect, actions, resources, conditions}: the role's shape, not secrets."""
    out: list[dict[str, Any]] = []
    for statement in _as_list(doc.get("Statement"))[:max_statements]:
        if not isinstance(statement, dict):
            continue
        out.append(
            {
                "effect": statement.get("Effect"),
                "actions": [str(a) for a in _as_list(statement.get("Action"))][:40],
                "resources": [str(r) for r in _as_list(statement.get("Resource"))][:40],
                "conditions": statement.get("Condition") or {},
            }
        )
    return out


def github_oidc_subjects(trust: dict[str, Any]) -> list[str]:
    """`sub` values a trust policy accepts from GitHub's OIDC provider (repo:owner/name:...)."""
    subjects: set[str] = set()
    for statement in _as_list(trust.get("Statement")):
        if not isinstance(statement, dict):
            continue
        principal = statement.get("Principal") or {}
        federated = _as_list(principal.get("Federated")) if isinstance(principal, dict) else []
        if not any("token.actions.githubusercontent.com" in str(p) for p in federated):
            continue
        for values in (statement.get("Condition") or {}).values():
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                if key.endswith(":sub"):
                    subjects.update(str(v) for v in _as_list(value))
    return sorted(subjects)


def repos_from_subjects(subjects: Iterable[str]) -> list[str]:
    """owner/name from GitHub OIDC subjects, including the immutable owner@id/name@id form."""
    repos: set[str] = set()
    for sub in subjects:
        match = re.match(r"^repo:([^:]+):", sub)
        if not match:
            continue
        owner, _, name = match.group(1).partition("/")
        repos.add(f"{owner.split('@')[0]}/{name.split('@')[0]}")
    return sorted(repos)


# ------------------------------------------------------------------------- normalizers


def _arn_parts(arn: str) -> tuple[str, str, str, str]:
    """(service, region, account, resource) of an ARN."""
    parts = arn.split(":", 5)
    if len(parts) < 6:
        return "", "", "", arn
    return parts[2], parts[3], parts[4], parts[5]


def _name_from_arn(arn: str) -> str:
    resource = _arn_parts(arn)[3]
    return resource.rsplit("/", 1)[-1].rsplit(":", 1)[-1] or arn


def normalize_cluster(cluster: dict[str, Any], region: str, account: str) -> DiscoveredResource:
    return DiscoveredResource(
        provider=PROVIDER,
        type="ecs:cluster",
        external_id=cluster["clusterArn"],
        name=cluster.get("clusterName") or _name_from_arn(cluster["clusterArn"]),
        account=account,
        region=region,
        tags=_tags(cluster.get("tags")),
        config={
            "status": cluster.get("status"),
            "running_tasks": cluster.get("runningTasksCount"),
            "active_services": cluster.get("activeServicesCount"),
            "container_insights": next(
                (
                    s.get("value")
                    for s in cluster.get("settings", [])
                    if s.get("name") == "containerInsights"
                ),
                None,
            ),
        },
    )


def container_summary(container: dict[str, Any]) -> dict[str, Any]:
    env_names, env_values = env_summary(container.get("environment", []))
    log = container.get("logConfiguration") or {}
    options = log.get("options") or {}
    image = str(container.get("image", ""))
    return {
        "name": container.get("name"),
        "image": image,
        **{f"image_{k}": v for k, v in parse_image(image).items()},
        "ports": [
            {"port": p.get("containerPort"), "protocol": p.get("protocol", "tcp")}
            for p in container.get("portMappings", [])
        ],
        "env_names": env_names,
        "env_values": env_values,
        # Secret references: env name and the ARN it comes from. Never the value.
        "secrets": [
            {"name": s.get("name"), "value_from": s.get("valueFrom")}
            for s in container.get("secrets", [])
        ],
        "log": {
            "driver": log.get("logDriver"),
            "group": options.get("awslogs-group"),
            "region": options.get("awslogs-region"),
            "stream_prefix": options.get("awslogs-stream-prefix"),
        }
        if log
        else None,
        "health_check": bool(container.get("healthCheck")),
        "memory": container.get("memory"),
    }


def task_summary(task: dict[str, Any], eni_ips: dict[str, dict[str, str | None]]) -> dict[str, Any]:
    eni_id = None
    for attachment in task.get("attachments", []):
        for detail in attachment.get("details", []):
            if detail.get("name") == "networkInterfaceId":
                eni_id = detail.get("value")
    ips = eni_ips.get(eni_id or "", {})
    return {
        "task_arn": task.get("taskArn"),
        "task_definition": task.get("taskDefinitionArn"),
        "last_status": task.get("lastStatus"),
        "health": task.get("healthStatus"),
        "started_at": _iso(task.get("startedAt")),
        "stopped_reason": task.get("stoppedReason"),
        "eni": eni_id,
        "public_ip": ips.get("public_ip"),
        "private_ip": ips.get("private_ip"),
        "images": [
            {"name": c.get("name"), "image": c.get("image"), "digest": c.get("imageDigest")}
            for c in task.get("containers", [])
        ],
    }


def normalize_service(
    service: dict[str, Any],
    task_definition: dict[str, Any] | None,
    tasks: list[dict[str, Any]],
    eni_ips: dict[str, dict[str, str | None]],
    region: str,
    account: str,
) -> DiscoveredResource:
    network = (service.get("networkConfiguration") or {}).get("awsvpcConfiguration") or {}
    td = task_definition or {}
    running = [task_summary(t, eni_ips) for t in tasks]
    return DiscoveredResource(
        provider=PROVIDER,
        type="ecs:service",
        external_id=service["serviceArn"],
        name=service.get("serviceName") or _name_from_arn(service["serviceArn"]),
        account=account,
        region=region,
        tags=_tags(service.get("tags")),
        config={
            "cluster_arn": service.get("clusterArn"),
            "status": service.get("status"),
            "launch_type": service.get("launchType"),
            "desired_count": service.get("desiredCount"),
            "running_count": service.get("runningCount"),
            "pending_count": service.get("pendingCount"),
            "task_definition": service.get("taskDefinition"),
            "load_balancers": [
                {
                    "target_group": lb.get("targetGroupArn"),
                    "container": lb.get("containerName"),
                    "port": lb.get("containerPort"),
                }
                for lb in service.get("loadBalancers", [])
            ],
            "has_load_balancer": bool(service.get("loadBalancers")),
            "network": {
                "subnets": network.get("subnets", []),
                "security_groups": network.get("securityGroups", []),
                "assign_public_ip": network.get("assignPublicIp") == "ENABLED",
            },
            "deployments": [
                {
                    "status": d.get("status"),
                    "rollout_state": d.get("rolloutState"),
                    "rollout_reason": d.get("rolloutStateReason"),
                    "task_definition": d.get("taskDefinition"),
                    "desired": d.get("desiredCount"),
                    "running": d.get("runningCount"),
                    "failed_tasks": d.get("failedTasks"),
                    "created_at": _iso(d.get("createdAt")),
                    "updated_at": _iso(d.get("updatedAt")),
                }
                for d in service.get("deployments", [])[:5]
            ],
            "events": [
                {"at": _iso(e.get("createdAt")), "message": redact(str(e.get("message", "")))}
                for e in service.get("events", [])[:8]
            ],
            "circuit_breaker": (service.get("deploymentConfiguration") or {}).get(
                "deploymentCircuitBreaker"
            ),
            "task_family": td.get("family"),
            "task_revision": td.get("revision"),
            "cpu": td.get("cpu"),
            "memory": td.get("memory"),
            "execution_role_arn": td.get("executionRoleArn"),
            "task_role_arn": td.get("taskRoleArn"),
            "containers": [container_summary(c) for c in td.get("containerDefinitions", [])],
            "running_tasks": running,
            "public_ips": sorted({t["public_ip"] for t in running if t.get("public_ip")}),
        },
    )


def normalize_repository(
    repo: dict[str, Any], images: list[dict[str, Any]], region: str, account: str
) -> DiscoveredResource:
    latest = sorted(images, key=lambda i: str(i.get("imagePushedAt", "")), reverse=True)[:5]
    return DiscoveredResource(
        provider=PROVIDER,
        type="ecr:repository",
        external_id=repo["repositoryArn"],
        name=repo["repositoryName"],
        account=account,
        region=region,
        config={
            "uri": repo.get("repositoryUri"),
            "tag_mutability": repo.get("imageTagMutability"),
            "scan_on_push": (repo.get("imageScanningConfiguration") or {}).get("scanOnPush"),
            "images": [
                {
                    "tags": i.get("imageTags", []),
                    "digest": i.get("imageDigest"),
                    "pushed_at": _iso(i.get("imagePushedAt")),
                    "size_bytes": i.get("imageSizeInBytes"),
                }
                for i in latest
            ],
        },
    )


def normalize_db(db: dict[str, Any], region: str, account: str) -> DiscoveredResource:
    endpoint = db.get("Endpoint") or {}
    return DiscoveredResource(
        provider=PROVIDER,
        type="rds:db",
        external_id=db["DBInstanceArn"],
        name=db["DBInstanceIdentifier"],
        account=account,
        region=region,
        tags=_tags(db.get("TagList")),
        config={
            "engine": db.get("Engine"),
            "engine_version": db.get("EngineVersion"),
            "instance_class": db.get("DBInstanceClass"),
            "status": db.get("DBInstanceStatus"),
            "endpoint": {"address": endpoint.get("Address"), "port": endpoint.get("Port")},
            "publicly_accessible": bool(db.get("PubliclyAccessible")),
            "multi_az": db.get("MultiAZ"),
            "storage_encrypted": db.get("StorageEncrypted"),
            "deletion_protection": db.get("DeletionProtection"),
            "vpc_id": (db.get("DBSubnetGroup") or {}).get("VpcId"),
            "security_groups": [
                g.get("VpcSecurityGroupId") for g in db.get("VpcSecurityGroups", [])
            ],
            "db_name": db.get("DBName"),
        },
    )


def normalize_vpc(
    vpc: dict[str, Any], subnets: list[dict[str, Any]], region: str, account: str
) -> DiscoveredResource:
    tags = _tags(vpc.get("Tags"))
    return DiscoveredResource(
        provider=PROVIDER,
        type="ec2:vpc",
        external_id=f"arn:aws:ec2:{region}:{account}:vpc/{vpc['VpcId']}",
        name=tags.get("Name") or vpc["VpcId"],
        account=account,
        region=region,
        tags=tags,
        config={
            "vpc_id": vpc["VpcId"],
            "cidr": vpc.get("CidrBlock"),
            "is_default": vpc.get("IsDefault"),
            "subnets": [
                {
                    "id": s.get("SubnetId"),
                    "az": s.get("AvailabilityZone"),
                    "cidr": s.get("CidrBlock"),
                    "public_ip_on_launch": s.get("MapPublicIpOnLaunch"),
                    "name": _tags(s.get("Tags")).get("Name"),
                }
                for s in subnets
                if s.get("VpcId") == vpc["VpcId"]
            ],
        },
    )


def _rule(permission: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol": permission.get("IpProtocol"),
        "from_port": permission.get("FromPort"),
        "to_port": permission.get("ToPort"),
        "cidrs": [r.get("CidrIp") for r in permission.get("IpRanges", [])]
        + [r.get("CidrIpv6") for r in permission.get("Ipv6Ranges", [])],
        "source_groups": [g.get("GroupId") for g in permission.get("UserIdGroupPairs", [])],
        "descriptions": [
            d
            for d in [r.get("Description") for r in permission.get("IpRanges", [])]
            + [g.get("Description") for g in permission.get("UserIdGroupPairs", [])]
            if d
        ],
    }


def port_text(rule: dict[str, Any]) -> str:
    if rule.get("protocol") == "-1":
        return "all ports"
    low, high = rule.get("from_port"), rule.get("to_port")
    return str(low) if low == high else f"{low}-{high}"


def normalize_security_group(sg: dict[str, Any], region: str, account: str) -> DiscoveredResource:
    ingress = [_rule(p) for p in sg.get("IpPermissions", [])]
    open_ports = [
        port_text(r) for r in ingress if "0.0.0.0/0" in r["cidrs"] or "::/0" in r["cidrs"]
    ]
    return DiscoveredResource(
        provider=PROVIDER,
        type="ec2:security-group",
        external_id=f"arn:aws:ec2:{region}:{account}:security-group/{sg['GroupId']}",
        name=sg.get("GroupName") or sg["GroupId"],
        account=account,
        region=region,
        tags=_tags(sg.get("Tags")),
        config={
            "group_id": sg["GroupId"],
            "vpc_id": sg.get("VpcId"),
            "description": sg.get("Description"),
            "ingress": ingress,
            "egress": [_rule(p) for p in sg.get("IpPermissionsEgress", [])],
            "open_to_world": open_ports,
        },
    )


def normalize_function(fn: dict[str, Any], region: str, account: str) -> DiscoveredResource:
    env = (fn.get("Environment") or {}).get("Variables") or {}
    env_names, env_values = env_summary({"name": k, "value": v} for k, v in env.items())
    logging_config = fn.get("LoggingConfig") or {}
    return DiscoveredResource(
        provider=PROVIDER,
        type="lambda:function",
        external_id=fn["FunctionArn"],
        name=fn["FunctionName"],
        account=account,
        region=region,
        config={
            "runtime": fn.get("Runtime"),
            "package_type": fn.get("PackageType"),
            "handler": fn.get("Handler"),
            "role": fn.get("Role"),
            "memory": fn.get("MemorySize"),
            "timeout": fn.get("Timeout"),
            "last_modified": fn.get("LastModified"),
            "env_names": env_names,
            "env_values": env_values,
            "security_groups": (fn.get("VpcConfig") or {}).get("SecurityGroupIds", []),
            "log_group": logging_config.get("LogGroup") or f"/aws/lambda/{fn['FunctionName']}",
        },
    )


def normalize_log_group(group: dict[str, Any], region: str, account: str) -> DiscoveredResource:
    arn = str(
        group.get("arn") or f"arn:aws:logs:{region}:{account}:log-group:{group['logGroupName']}"
    )
    return DiscoveredResource(
        provider=PROVIDER,
        type="logs:log-group",
        external_id=arn.removesuffix(":*"),
        name=group["logGroupName"],
        account=account,
        region=region,
        config={
            "retention_days": group.get("retentionInDays"),
            "stored_bytes": group.get("storedBytes"),
            "created_at": group.get("creationTime"),
        },
    )


def normalize_secret(secret: dict[str, Any], region: str, account: str) -> DiscoveredResource:
    """Metadata only (ListSecrets / DescribeSecret). There is no value here to leak."""
    return DiscoveredResource(
        provider=PROVIDER,
        type="secretsmanager:secret",
        external_id=secret["ARN"],
        name=secret["Name"],
        account=account,
        region=region,
        tags=_tags(secret.get("Tags")),
        config={
            "description": secret.get("Description"),
            "last_changed_at": _iso(secret.get("LastChangedDate")),
            "last_accessed_at": _iso(secret.get("LastAccessedDate")),
            "created_at": _iso(secret.get("CreatedDate")),
            "rotation_enabled": bool(secret.get("RotationEnabled")),
            "kms_key": "customer managed" if secret.get("KmsKeyId") else "aws managed",
            "version_stages": {
                vid: stages  # the full id: Terraform ids all start "terraform-"
                for vid, stages in (secret.get("SecretVersionsToStages") or {}).items()
            },
        },
    )


def normalize_role(
    role: dict[str, Any],
    inline: dict[str, dict[str, Any]],
    attached: list[str],
    account: str,
) -> DiscoveredResource:
    trust = policy_document(role.get("AssumeRolePolicyDocument"))
    subjects = github_oidc_subjects(trust)
    services = sorted(
        {
            str(s)
            for st in _as_list(trust.get("Statement"))
            if isinstance(st, dict) and isinstance(st.get("Principal"), dict)
            for s in _as_list(st["Principal"].get("Service"))
        }
    )
    return DiscoveredResource(
        provider=PROVIDER,
        type="iam:role",
        external_id=role["Arn"],
        name=role["RoleName"],
        account=account,
        region=None,
        tags=_tags(role.get("Tags")),
        config={
            "description": role.get("Description"),
            "max_session_seconds": role.get("MaxSessionDuration"),
            "trusted_services": services,
            "github_oidc_subjects": subjects,
            "github_repos": repos_from_subjects(subjects),
            "inline_policies": {name: summarize_policy(doc) for name, doc in inline.items()},
            "attached_policies": attached,
            "created_at": _iso(role.get("CreateDate")),
        },
    )


_FLAT_TYPES = {"s3": "s3:bucket", "sqs": "sqs:queue", "sns": "sns:topic"}


def inventory_type(arn: str) -> str:
    """Resource Explorer style type ("ecs:service", "rds:db") for an ARN from the tagging API,
    which has no type field."""
    service, _, _, resource = _arn_parts(arn)
    if service in _FLAT_TYPES:
        return _FLAT_TYPES[service]
    if service == "elasticloadbalancing" and resource.startswith("loadbalancer/"):
        kind = resource.split("/")[1]
        return (
            f"elasticloadbalancing:loadbalancer/{kind}"
            if kind in ("app", "net")
            else ("elasticloadbalancing:loadbalancer")
        )
    return f"{service}:{re.split(r'[/:]', resource, maxsplit=1)[0]}"


def normalize_inventory_item(item: dict[str, Any], account: str) -> DiscoveredResource:
    """A Resource Explorer or tagging API item with no dedicated describe."""
    arn = str(item.get("Arn") or item.get("ResourceARN"))
    _, region, item_account, _ = _arn_parts(arn)
    tags: dict[str, str] = {}
    for prop in item.get("Properties", []):
        if prop.get("Name") == "tags":
            tags = _tags(prop.get("Data"))
    if "Tags" in item:
        tags = _tags(item["Tags"])
    rtype = str(item.get("ResourceType") or inventory_type(arn))
    return DiscoveredResource(
        provider=PROVIDER,
        type=rtype,
        external_id=arn,
        name=tags.get("Name") or _name_from_arn(arn),
        account=item_account or account,
        region=item.get("Region") or region or None,
        tags=tags,
        config={"source": "inventory"},
    )


# --------------------------------------------------------------------------- collector


class AwsDiscovery:
    """Runs the calls (in threads) and normalizes. One instance per discovery."""

    def __init__(
        self,
        reader: AwsReader,
        account: str,
        *,
        max_resources: int,
        progress: Progress = no_progress,
    ) -> None:
        self.reader = reader
        self.account = account
        self.max_resources = max_resources
        self.progress = progress
        self.notes: list[str] = []
        self.by_arn: dict[str, DiscoveredResource] = {}
        self.inventory: dict[str, dict[str, Any]] = {}

    def _note(self, text: str) -> None:
        self.notes.append(text)
        logger.info("aws discovery note", extra={"note": text})

    def _add(self, resources: Iterable[DiscoveredResource]) -> None:
        for resource in resources:
            existing = self.by_arn.get(resource.external_id)
            if existing is not None and existing.config.get("source") != "inventory":
                continue
            if existing is not None:
                resource.tags = {**existing.tags, **resource.tags}
            listed = self.inventory.get(resource.external_id)
            if listed is not None:
                # Inventory tags (Resource Explorer) for resources whose describe has none.
                inventory_tags = normalize_inventory_item(listed, self.account).tags
                resource.tags = {**inventory_tags, **resource.tags}
            self.by_arn[resource.external_id] = resource

    async def _step(self, label: str, fn: Callable[[], list[DiscoveredResource]]) -> None:
        started = time.perf_counter()
        try:
            found = await asyncio.to_thread(fn)
        except Exception as exc:
            reason = error_text(exc)
            self._note(f"{label}: skipped ({reason})")
            await self.progress(f"{label}: skipped ({reason})")
            return
        self._add(found)
        ms = round((time.perf_counter() - started) * 1000)
        await self.progress(f"{label}: {len(found)} found ({ms} ms)")

    async def run(self) -> ConnectorResult:
        for region in self.reader.regions:
            await self.progress(f"AWS {self.account} {region}: reading inventory")
            await self._step(f"Inventory {region}", functools.partial(self.inventory_items, region))
            await asyncio.gather(
                self._step(f"ECS {region}", functools.partial(self.ecs, region)),
                self._step(f"ECR {region}", functools.partial(self.ecr, region)),
                self._step(f"RDS {region}", functools.partial(self.rds, region)),
                self._step(
                    f"VPC and security groups {region}", functools.partial(self.network, region)
                ),
                self._step(f"Lambda {region}", functools.partial(self.functions, region)),
                self._step(f"Log groups {region}", functools.partial(self.log_groups, region)),
                self._step(f"Secrets (metadata) {region}", functools.partial(self.secrets, region)),
            )
            await self._step(
                f"Cloud Control {region}", functools.partial(self.cloud_control, region)
            )
        await self._step("IAM roles", self.roles)

        resources = sorted(self.by_arn.values(), key=lambda r: (r.type, r.name))
        truncated = len(resources) > self.max_resources
        if truncated:
            self._note(
                f"Found {len(resources)} resources; kept the first {self.max_resources} "
                "(MAX_RESOURCES_PER_CONNECTION)."
            )
            resources = resources[: self.max_resources]
        return ConnectorResult(resources=resources, notes=self.notes, truncated=truncated)

    # ---- inventory

    def inventory_items(self, region: str) -> list[DiscoveredResource]:
        items: list[dict[str, Any]] = []
        try:
            view = self.reader.call("resource-explorer-2", region, "GetDefaultView").get("ViewArn")
            if not view:
                raise LookupError("no default view")
            items = self.reader.paginate(
                "resource-explorer-2",
                region,
                "Search",
                "Resources",
                limit=self.max_resources * 3,
                QueryString=f"region:{region}",
                ViewArn=view,
            )
        except Exception as exc:
            reason = error_code(exc) or error_text(exc)
            self._note(
                f"Resource Explorer is not set up in {region} ({reason}); "
                "used the tagging API instead."
            )
            items = self.reader.paginate(
                "resourcegroupstaggingapi",
                region,
                "GetResources",
                "ResourceTagMappingList",
                limit=self.max_resources * 3,
            )
        out: list[DiscoveredResource] = []
        for item in items:
            resource = normalize_inventory_item(item, self.account)
            self.inventory[resource.external_id] = item
            if resource.type in EXTRA_TYPES:
                out.append(resource)
        return out

    def cloud_control(self, region: str) -> list[DiscoveredResource]:
        calls = 0
        out: list[DiscoveredResource] = []
        for arn, resource in list(self.by_arn.items()):
            if resource.region != region or resource.type not in CLOUD_CONTROL_TYPES:
                continue
            if calls >= MAX_CLOUD_CONTROL_CALLS:
                self._note(f"Cloud Control: stopped after {MAX_CLOUD_CONTROL_CALLS} lookups.")
                break
            type_name, identifier = CLOUD_CONTROL_TYPES[resource.type]
            calls += 1
            try:
                desc = self.reader.call(
                    "cloudcontrol",
                    region,
                    "GetResource",
                    TypeName=type_name,
                    Identifier=identifier(arn),
                )
            except Exception as exc:
                logger.info("cloud control get failed", extra={"error": error_text(exc)})
                continue
            try:
                props = json.loads(desc.get("ResourceDescription", {}).get("Properties") or "{}")
            except ValueError:
                props = {}
            updated = resource.model_copy(deep=True)
            updated.config = {
                "source": "cloudcontrol",
                "cloudformation_type": type_name,
                "properties": redact_json(_trim(props)),
            }
            out.append(updated)
        return out

    # ---- ECS

    def ecs(self, region: str) -> list[DiscoveredResource]:
        r = self.reader
        cluster_arns = r.paginate("ecs", region, "ListClusters", "clusterArns")
        if not cluster_arns:
            return []
        clusters = r.call(
            "ecs",
            region,
            "DescribeClusters",
            clusters=cluster_arns[:100],
            include=["TAGS", "SETTINGS"],
        ).get("clusters", [])
        out = [normalize_cluster(c, region, self.account) for c in clusters]
        for cluster_arn in cluster_arns:
            service_arns = r.paginate(
                "ecs", region, "ListServices", "serviceArns", cluster=cluster_arn
            )
            services: list[dict[str, Any]] = []
            for chunk in _chunks(service_arns, 10):
                services += r.call(
                    "ecs",
                    region,
                    "DescribeServices",
                    cluster=cluster_arn,
                    services=chunk,
                    include=["TAGS"],
                ).get("services", [])
            task_arns = r.paginate(
                "ecs", region, "ListTasks", "taskArns", cluster=cluster_arn, desiredStatus="RUNNING"
            )
            tasks: list[dict[str, Any]] = []
            for chunk in _chunks(task_arns, 100):
                tasks += r.call(
                    "ecs", region, "DescribeTasks", cluster=cluster_arn, tasks=chunk
                ).get("tasks", [])
            eni_ids = [
                d["value"]
                for t in tasks
                for a in t.get("attachments", [])
                for d in a.get("details", [])
                if d.get("name") == "networkInterfaceId" and d.get("value")
            ]
            eni_ips = self._eni_ips(region, eni_ids)
            task_defs: dict[str, dict[str, Any]] = {}
            for service in services:
                td_arn = service.get("taskDefinition")
                if td_arn and td_arn not in task_defs:
                    task_defs[td_arn] = r.call(
                        "ecs", region, "DescribeTaskDefinition", taskDefinition=td_arn
                    ).get("taskDefinition", {})
                group = f"service:{service.get('serviceName')}"
                own = [t for t in tasks if t.get("group") == group]
                out.append(
                    normalize_service(
                        service, task_defs.get(td_arn or ""), own, eni_ips, region, self.account
                    )
                )
        return out

    def _eni_ips(self, region: str, eni_ids: list[str]) -> dict[str, dict[str, str | None]]:
        out: dict[str, dict[str, str | None]] = {}
        for chunk in _chunks(eni_ids, 200):
            enis = self.reader.call(
                "ec2", region, "DescribeNetworkInterfaces", NetworkInterfaceIds=chunk
            ).get("NetworkInterfaces", [])
            for eni in enis:
                out[eni["NetworkInterfaceId"]] = {
                    "public_ip": (eni.get("Association") or {}).get("PublicIp"),
                    "private_ip": eni.get("PrivateIpAddress"),
                }
        return out

    # ---- the rest

    def ecr(self, region: str) -> list[DiscoveredResource]:
        repos = self.reader.paginate("ecr", region, "DescribeRepositories", "repositories")
        out = []
        for repo in repos:
            images = self.reader.paginate(
                "ecr",
                region,
                "DescribeImages",
                "imageDetails",
                limit=100,
                repositoryName=repo["repositoryName"],
            )
            out.append(normalize_repository(repo, images, region, self.account))
        return out

    def rds(self, region: str) -> list[DiscoveredResource]:
        dbs = self.reader.paginate("rds", region, "DescribeDBInstances", "DBInstances")
        return [normalize_db(db, region, self.account) for db in dbs]

    def network(self, region: str) -> list[DiscoveredResource]:
        vpcs = self.reader.paginate("ec2", region, "DescribeVpcs", "Vpcs")
        subnets = self.reader.paginate("ec2", region, "DescribeSubnets", "Subnets")
        groups = self.reader.paginate("ec2", region, "DescribeSecurityGroups", "SecurityGroups")
        return [
            normalize_vpc(v, subnets, region, self.account) for v in vpcs if not v.get("IsDefault")
        ] + [
            normalize_security_group(g, region, self.account)
            for g in groups
            if g.get("GroupName") != "default"
        ]

    def functions(self, region: str) -> list[DiscoveredResource]:
        fns = self.reader.paginate("lambda", region, "ListFunctions", "Functions")
        out = [normalize_function(f, region, self.account) for f in fns]
        # ListFunctions has no tags; groundcrew:role=tooling and the rest come from ListTags
        # (lambda:ListTags is in the read-only role). The inventory adds tags too, when it
        # lists the function.
        for resource in out[:MAX_TAG_CALLS]:
            try:
                tags = self.reader.call(
                    "lambda", region, "ListTags", Resource=resource.external_id
                ).get("Tags")
            except Exception as exc:
                logger.info("lambda tags failed", extra={"error": error_text(exc)})
                continue
            resource.tags = {**resource.tags, **{str(k): str(v) for k, v in (tags or {}).items()}}
        return out

    def log_groups(self, region: str) -> list[DiscoveredResource]:
        groups = self.reader.paginate("logs", region, "DescribeLogGroups", "logGroups", limit=500)
        return [normalize_log_group(g, region, self.account) for g in groups]

    def secrets(self, region: str) -> list[DiscoveredResource]:
        items = self.reader.paginate("secretsmanager", region, "ListSecrets", "SecretList")
        return [normalize_secret(s, region, self.account) for s in items]

    def roles(self) -> list[DiscoveredResource]:
        """Roles tasks run as, and roles GitHub Actions can assume (OIDC)."""
        wanted: set[str] = set()
        for resource in self.by_arn.values():
            if resource.type == "ecs:service":
                for key in ("execution_role_arn", "task_role_arn"):
                    if resource.config.get(key):
                        wanted.add(str(resource.config[key]).rsplit("/", 1)[-1])
            if resource.type == "lambda:function" and resource.config.get("role"):
                wanted.add(str(resource.config["role"]).rsplit("/", 1)[-1])
        all_roles = self.reader.paginate("iam", None, "ListRoles", "Roles")
        picked: list[dict[str, Any]] = []
        for role in all_roles:
            trust = policy_document(role.get("AssumeRolePolicyDocument"))
            if role["RoleName"] in wanted or github_oidc_subjects(trust):
                picked.append(role)
        out = []
        for listed in picked:
            name = listed["RoleName"]
            # ListRoles leaves out tags; GetRole has them (groundcrew:role=tooling).
            try:
                role = {**listed, **self.reader.call("iam", None, "GetRole", RoleName=name)["Role"]}
            except Exception as exc:
                logger.info("iam get role failed", extra={"error": error_text(exc)})
                role = listed
            inline: dict[str, dict[str, Any]] = {}
            for policy_name in self.reader.paginate(
                "iam", None, "ListRolePolicies", "PolicyNames", RoleName=name
            )[:10]:
                doc = self.reader.call(
                    "iam", None, "GetRolePolicy", RoleName=name, PolicyName=policy_name
                ).get("PolicyDocument")
                inline[policy_name] = policy_document(doc)
            attached = [
                p.get("PolicyName", "")
                for p in self.reader.paginate(
                    "iam", None, "ListAttachedRolePolicies", "AttachedPolicies", RoleName=name
                )
            ]
            out.append(normalize_role(role, inline, attached, self.account))
        return out


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _trim(value: Any, depth: int = 0) -> Any:
    """Keep Cloud Control properties small: shallow, short lists, short strings."""
    if depth > 3:
        return "..."
    if isinstance(value, dict):
        return {str(k): _trim(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return [_trim(v, depth + 1) for v in value[:10]]
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + "..."
    return value
