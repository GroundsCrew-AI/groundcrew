"""The deploy infrastructure contract (Phase 4): names, tags, and network for what Groundcrew
creates in a customer's AWS account or Kubernetes cluster. docs/phase4-infra.md explains it.

Everything Groundcrew creates is named under a groundcrew prefix and tagged
project=groundcrew plus groundcrew:deploy=<run_id>. The deploy role (infra/aws-connect.yaml,
Tier=deploy), the kind admission policy (infra/kind/), and `make cleanup-deploys` rely on
exactly these names and tags, so targets build them here instead of by hand.
"""

import re
import uuid
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings

PROJECT_TAG = "project"
PROJECT_VALUE = "groundcrew"
DEPLOY_TAG = "groundcrew:deploy"
CLUSTER_NAME = "groundcrew-deploys"
TASK_EXECUTION_ROLE = "groundcrew-task-execution"
DEPLOY_ROLE = "groundcrew-deploy"

# DNS-1123 label (namespaces) is the strictest rule of the three kinds of name, so one slug
# works for all: lowercase letters, digits, and hyphens.
_SLUG = re.compile(r"[^a-z0-9-]+")
_MAX_NAMESPACE = 63


class DeployInfraError(RuntimeError):
    """The deploy infrastructure is missing or misconfigured. The message says what to do."""


def required_tags(run_id: uuid.UUID | str) -> dict[str, str]:
    """Tags every resource a deploy creates must carry (cleanup and the IAM conditions use them)."""
    return {PROJECT_TAG: PROJECT_VALUE, DEPLOY_TAG: str(run_id)}


def aws_tags(run_id: uuid.UUID | str) -> list[dict[str, str]]:
    """required_tags in the AWS shape for ECS, ECR, and EC2 ([{"Key": ..., "Value": ...}])."""
    return [{"Key": k, "Value": v} for k, v in required_tags(run_id).items()]


K8S_DEPLOY_LABEL = "groundcrew/deploy"
# Pinned: `make cleanup-deploys` never deletes a resource tagged groundcrew:keep=true (AWS) or
# labelled groundcrew/keep=true (Kubernetes), whatever its age.
KEEP_TAG = "groundcrew:keep"
K8S_KEEP_LABEL = "groundcrew/keep"
KEEP_VALUE = "true"


def k8s_labels(run_id: uuid.UUID | str) -> dict[str, str]:
    """required_tags as Kubernetes labels: project=groundcrew and groundcrew/deploy=<run_id>
    (a label name cannot contain ":")."""
    return {PROJECT_TAG: PROJECT_VALUE, K8S_DEPLOY_LABEL: str(run_id)}


def slug(app: str) -> str:
    """An app name made safe for ECR, log group, and namespace names."""
    value = _SLUG.sub("-", app.lower()).strip("-")
    value = re.sub(r"-{2,}", "-", value)
    if not value:
        raise DeployInfraError(f"App name {app!r} has no letters or digits to name resources with.")
    return value


def _fit(prefix: str, app: str, limit: int) -> str:
    name = f"{prefix}{slug(app)}"
    return name[:limit].rstrip("-")


def ecr_repository(settings: Settings, app: str) -> str:
    """ECR repository for an app: groundcrew-<app>."""
    return _fit(settings.gc_ecr_prefix, app, 256)


def log_group(settings: Settings, app: str) -> str:
    """CloudWatch log group for an app: /groundcrew/<app>."""
    return _fit(settings.gc_log_group_prefix, app, 512)


def namespace(settings: Settings, app: str) -> str:
    """Kubernetes namespace for an app: gc-<app>, at most 63 characters."""
    return _fit(settings.gc_namespace_prefix, app, _MAX_NAMESPACE)


def ecs_service(app: str) -> str:
    """ECS service name for an app, inside the groundcrew-deploys cluster."""
    return _fit("", app, 255)


def task_family(app: str) -> str:
    """Task definition family for an app: groundcrew-<app>. The deploy role may only run task
    definitions whose family starts with groundcrew-."""
    return _fit("groundcrew-", app, 255)


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def secret_name(app: str, var: str) -> str:
    """Secrets Manager name for one env secret of an app: groundcrew/<app>/<VAR>. The deploy
    role may create, update, and delete only groundcrew/* secrets (never read them); the task
    execution role reads them when ECS starts a task."""
    if not _ENV_NAME.fullmatch(var):
        raise DeployInfraError(f"{var!r} is not an environment variable name.")
    return f"groundcrew/{slug(app)}/{var}"


def deploy_role_arn(account_id: str) -> str:
    """The groundcrew-deploy role a Tier=deploy stack creates in that account."""
    if not re.fullmatch(r"\d{12}", account_id):
        raise DeployInfraError(f"Not an AWS account id: {account_id!r}.")
    return f"arn:aws:iam::{account_id}:role/{DEPLOY_ROLE}"


def task_execution_role_arn(settings: Settings, account_id: str) -> str:
    """The task execution role ARN: the setting, or the stack's fixed name in that account."""
    if settings.gc_task_execution_role_arn:
        return settings.gc_task_execution_role_arn
    if not re.fullmatch(r"\d{12}", account_id):
        raise DeployInfraError(f"Not an AWS account id: {account_id!r}.")
    return f"arn:aws:iam::{account_id}:role/{TASK_EXECUTION_ROLE}"


def resolve_subnets(session: boto3.Session, settings: Settings) -> list[str]:
    """Subnets for Fargate tasks: GC_SUBNET_IDS if set (checked to exist), otherwise the
    default VPC's default subnets in GC_AWS_REGION. Raises DeployInfraError with the fix when
    neither is usable. Read-only calls (ec2:DescribeVpcs, ec2:DescribeSubnets)."""
    region = settings.gc_aws_region
    ec2: Any = session.client("ec2", region_name=region)
    try:
        if settings.gc_subnet_ids:
            found = ec2.describe_subnets(SubnetIds=settings.gc_subnet_ids)["Subnets"]
            missing = set(settings.gc_subnet_ids) - {s["SubnetId"] for s in found}
            if missing:
                raise DeployInfraError(
                    f"GC_SUBNET_IDS has subnets that do not exist in {region}: "
                    f"{', '.join(sorted(missing))}."
                )
            return list(settings.gc_subnet_ids)
        vpcs = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"]
        if not vpcs:
            raise DeployInfraError(
                f"There is no default VPC in {region}, so Groundcrew does not know where to run "
                "tasks. Create one (aws ec2 create-default-vpc --region "
                f"{region}) or set GC_SUBNET_IDS to subnets with a route to the internet."
            )
        vpc_id = vpcs[0]["VpcId"]
        subnets = ec2.describe_subnets(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "default-for-az", "Values": ["true"]},
            ]
        )["Subnets"]
    except (ClientError, BotoCoreError) as exc:
        raise DeployInfraError(f"Could not read subnets in {region}: {exc}") from exc
    if not subnets:
        raise DeployInfraError(
            f"The default VPC {vpc_id} in {region} has no default subnets. Set GC_SUBNET_IDS."
        )
    return sorted(s["SubnetId"] for s in subnets)
