"""Smoke test of the AWS deploy tier on real AWS: deploy a tiny app with the groundcrew-deploy
role, prove what it must not do, and clean up.

    make smoke-ecs CONNECTION=<aws connection id>             # DRY_RUN=1: read-only checks + plan
    make smoke-ecs CONNECTION=<aws connection id> DRY_RUN=0   # the real run (costs < $0.01)
    make smoke-ecs CONNECTION=<aws connection id> CLEANUP=1   # remove leftovers of a failed run

Every AWS call uses ConnectionCredentialsProvider for that connection: aws_deploy_session()
(the deploy role, guarded by AWS_DEPLOY_OPERATIONS) and, for one read-only check,
aws_session(). Groundcrew's platform identity assumes the roles; never a profile or ~/.aws.
The only exception is `negative_checks`: five hardcoded requests sent with the same deploy-role
credentials through clients without the code allowlist, so the refusal comes from AWS IAM.

Everything it creates is tagged project=groundcrew and groundcrew:deploy=smoke-<UTC time>, and
cleanup deletes only what carries this run's tag. The dummy secret value and the registry
password never reach output, logs, or the report: everything printed or saved goes through
`Scrubber`, which refuses to emit them (the container logs only a hash of the value).
docs/deploy-live.md explains the steps and how to read the table.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

import boto3  # noqa: E402
from botocore.exceptions import BotoCoreError, ClientError, WaiterError  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402
from app.connectors.aws import ReadOnlyViolationError  # noqa: E402
from app.connectors.deploy_infra import (  # noqa: E402
    DEPLOY_TAG,
    PROJECT_TAG,
    PROJECT_VALUE,
    resolve_subnets,
    task_execution_role_arn,
)

# Fixed names: the deploy role allows exactly these prefixes (docs/phase4-infra.md).
REPO = "groundcrew-smoke"
LOG_GROUP = "/groundcrew/smoke"
SECRET = "groundcrew/smoke/TEST_VAR"  # noqa: S105 (a secret name, not a value)
FAMILY = "groundcrew-smoke"
SERVICE = "smoke"
CONTAINER = "smoke"
SG_PREFIX = "groundcrew-smoke-"
SOURCE_IMAGE = "nginx:1.27-alpine"
# The container proves the secret arrived without ever printing it: "set" plus a short hash.
CONTAINER_SCRIPT = (
    'if [ -n "$TEST_VAR" ]; then '
    'echo "TEST_VAR=set sha256:$(printf %s "$TEST_VAR" | sha256sum | cut -c1-12)"; '
    "else echo TEST_VAR=missing; fi; "
    'exec nginx -g "daemon off;"'
)
HASH_CHARS = 12

# Estimates for us-east-2, Linux/x86 on-demand (AWS pricing pages; check before budgeting).
PRICE_VCPU_HOUR = 0.04048
PRICE_GB_HOUR = 0.004445
PRICE_IPV4_HOUR = 0.005
PRICE_SECRET_MONTH = 0.40
TASK_VCPU, TASK_GB = 0.25, 0.5

STABLE_TIMEOUT_SECONDS = 600
HTTP_TIMEOUT_SECONDS = 90
LOG_TIMEOUT_SECONDS = 90
SG_DELETE_TIMEOUT_SECONDS = 180
SECRET_GONE_TIMEOUT_SECONDS = 60


# ------------------------------------------------------------------------------- output


class LeakError(RuntimeError):
    """Something tried to print or save a sensitive value."""


class NotOursError(RuntimeError):
    """A resource with a smoke name that this run (or Groundcrew) does not own: left alone."""


class Scrubber:
    """The single way text leaves this script. Sensitive values (the dummy secret, the
    registry password) are registered here; emitting text that contains one raises."""

    def __init__(self, out: Callable[[str], None] = print) -> None:
        self._values: list[str] = []
        self._out = out

    def register(self, value: str) -> None:
        if value:
            self._values.append(value)

    def clean(self, text: str) -> str:
        for value in self._values:
            text = text.replace(value, "***")
        return text

    def check(self, text: str) -> str:
        for value in self._values:
            if value in text:
                raise LeakError("refusing to output a sensitive value")
        return text

    def say(self, text: str) -> None:
        self._out(self.check(text))


@dataclass
class Step:
    name: str
    result: str  # PASS | FAIL | SKIP | GAP | INFO
    seconds: float
    note: str = ""


@dataclass
class Report:
    run_id: str
    dry_run: bool
    account: str = ""
    region: str = ""
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    steps: list[Step] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    manual_cleanup: list[str] = field(default_factory=list)
    leftovers: list[str] = field(default_factory=list)
    service_seconds: float = 0.0
    estimated_cost_usd: float = 0.0

    @property
    def ok(self) -> bool:
        return not any(s.result in ("FAIL", "GAP") for s in self.steps) and not self.leftovers


def estimate_cost(service_seconds: float, secret_seconds: float) -> float:
    hours = service_seconds / 3600
    task = hours * (TASK_VCPU * PRICE_VCPU_HOUR + TASK_GB * PRICE_GB_HOUR + PRICE_IPV4_HOUR)
    secret = PRICE_SECRET_MONTH * secret_seconds / (30 * 24 * 3600)
    return round(task + secret, 5)


def table(report: Report) -> str:
    rows = [("Step", "Result", "Seconds", "Note")] + [
        (s.name, s.result, f"{s.seconds:.1f}", s.note) for s in report.steps
    ]
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    lines = []
    for i, r in enumerate(rows):
        lines.append(f"{r[0]:<{widths[0]}}  {r[1]:<{widths[1]}}  {r[2]:>{widths[2]}}  {r[3]}")
        if i == 0:
            lines.append("-" * (sum(widths) + 4 + max(len(r[3]) for r in rows)))
    return "\n".join(lines)


def error_code(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code") or "ClientError")
    return type(exc).__name__


# ----------------------------------------------------------------------- negative checks

# Hardcoded. Each: (name, what must happen). These are the only requests in this file sent
# without the code allowlist; nothing else may build an unguarded client.
NEGATIVE_EXPECTED = {
    "a GetSecretValue groundcrew/smoke/TEST_VAR": {"AccessDeniedException"},
    "b CreateRepository smoke-untagged-name": {"AccessDeniedException"},
    "c CreateSecurityGroup without the project tag (DryRun)": {"UnauthorizedOperation"},
    "d CreateService in smoke-nonexistent-cluster": {"AccessDeniedException"},
    "e CreateRole groundcrew-smoke-should-not-exist": {"AccessDenied"},
}
# A result that means IAM let the request through (an IAM gap), per check.
NEGATIVE_GAP = {
    "a GetSecretValue groundcrew/smoke/TEST_VAR": {"allowed"},
    "b CreateRepository smoke-untagged-name": {"allowed"},
    "c CreateSecurityGroup without the project tag (DryRun)": {"DryRunOperation", "allowed"},
    "d CreateService in smoke-nonexistent-cluster": {"ClusterNotFoundException", "allowed"},
    "e CreateRole groundcrew-smoke-should-not-exist": {"allowed"},
}
# What to run by hand if a create was wrongly allowed (the deploy role cannot delete these).
NEGATIVE_CLEANUP = {
    "b CreateRepository smoke-untagged-name": (
        "aws ecr delete-repository --repository-name smoke-untagged-name --force --region {region}"
    ),
    "d CreateService in smoke-nonexistent-cluster": "",
    "e CreateRole groundcrew-smoke-should-not-exist": (
        "aws iam delete-role --role-name groundcrew-smoke-should-not-exist"
    ),
}


def negative_verdict(name: str, outcome: str) -> str:
    """PASS when AWS refused the way it must, GAP when IAM let it through, FAIL otherwise."""
    if outcome in NEGATIVE_EXPECTED[name]:
        return "PASS"
    if outcome in NEGATIVE_GAP[name]:
        return "GAP"
    return "FAIL"


def negative_checks(
    deploy: boto3.Session,
    region: str,
    vpc_id: str,
    task_definition: str,
    tags: list[dict[str, str]],
) -> list[tuple[str, str]]:
    """The five requests the deploy role must be refused, sent to AWS with the deploy role's own
    credentials but without the code allowlist (which would stop a and e before AWS sees them).
    Returns (check, outcome) where outcome is the AWS error code or "allowed". No response body
    is returned or printed (a would carry a secret value if it were wrongly allowed)."""
    creds = deploy.get_credentials().get_frozen_credentials()
    raw = boto3.Session(
        aws_access_key_id=creds.access_key,
        aws_secret_access_key=creds.secret_key,
        aws_session_token=creds.token,
        region_name=region,
    )
    names = list(NEGATIVE_EXPECTED)
    requests: list[tuple[str, Callable[[], Any]]] = [
        (names[0], lambda: raw.client("secretsmanager").get_secret_value(SecretId=SECRET)),
        (
            names[1],
            lambda: raw.client("ecr").create_repository(
                repositoryName="smoke-untagged-name", tags=tags
            ),
        ),
        (
            names[2],
            lambda: raw.client("ec2").create_security_group(
                GroupName=f"{SG_PREFIX}untagged",
                Description="Groundcrew smoke: must be refused (no project tag)",
                VpcId=vpc_id,
                DryRun=True,
            ),
        ),
        (
            names[3],
            lambda: raw.client("ecs").create_service(
                cluster="smoke-nonexistent-cluster",
                serviceName=SERVICE,
                taskDefinition=task_definition,
                desiredCount=0,
                tags=[{"key": t["Key"], "value": t["Value"]} for t in tags],
            ),
        ),
        (
            names[4],
            lambda: raw.client("iam").create_role(
                RoleName="groundcrew-smoke-should-not-exist",
                AssumeRolePolicyDocument=json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                                "Action": "sts:AssumeRole",
                            }
                        ],
                    }
                ),
            ),
        ),
    ]
    results = []
    for name, call in requests:
        try:
            call()
            outcome = "allowed"
        except ClientError as exc:
            outcome = error_code(exc)
        except BotoCoreError as exc:
            outcome = type(exc).__name__
        results.append((name, outcome))
    return results


# --------------------------------------------------------------------------- the smoke


@dataclass
class Created:
    """What this run made, so cleanup touches nothing else."""

    repository: bool = False
    log_group: bool = False
    security_group: str | None = None
    secret_arn: str | None = None
    service: bool = False
    task_definition: str | None = None


class Smoke:
    def __init__(
        self,
        deploy: boto3.Session,
        read: boto3.Session,
        settings: Settings,
        *,
        account: str,
        run_id: str,
        dry_run: bool,
        scrub: Scrubber,
        docker: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        public_ip: Callable[[], str] | None = None,
        http_get: Callable[[str], int] | None = None,
    ) -> None:
        self.deploy, self.read, self.settings = deploy, read, settings
        self.region = settings.gc_aws_region
        self.cluster = settings.gc_ecs_cluster
        self.account, self.run_id, self.dry_run = account, run_id, dry_run
        self.scrub = scrub
        self.report = Report(run_id=run_id, dry_run=dry_run, account=account, region=self.region)
        self.created = Created()
        self.tags = [
            {"Key": PROJECT_TAG, "Value": PROJECT_VALUE},
            {"Key": DEPLOY_TAG, "Value": run_id},
        ]
        self._docker = docker or _docker
        self._public_ip = public_ip or _public_ip
        self._http_get = http_get or _http_get
        self.vpc_id = ""
        self.subnets: list[str] = []
        self._secret_value = ""
        self._service_started = 0.0
        self._secret_created = 0.0
        self._leftovers_only = False

    def client(self, service: str) -> Any:
        return self.deploy.client(service, region_name=self.region)

    @contextmanager
    def step(self, name: str) -> Iterator[Step]:
        started = time.monotonic()
        record = Step(name, "PASS", 0.0)
        try:
            yield record
        except (ClientError, BotoCoreError, RuntimeError, WaiterError, OSError) as exc:
            record.result = "FAIL"
            record.note = self.scrub.clean(f"{error_code(exc)}: {exc}")[:300]
            raise
        finally:
            record.seconds = time.monotonic() - started
            record.note = self.scrub.clean(record.note)
            self.report.steps.append(record)
            self.scrub.say(f"  {record.result:<4} {name} ({record.seconds:.1f} s) {record.note}")

    # --- read-only prechecks (DRY_RUN=1 runs only these) -------------------------------

    def prechecks(self) -> bool:
        ok = True
        checks: list[tuple[str, Callable[[], str]]] = [
            ("identity is the deploy role", self._check_identity),
            (f"cluster {self.cluster} is ACTIVE", self._check_cluster),
            ("default VPC subnets", self._check_subnets),
            ("ECS service-linked role exists (read-only role)", self._check_slr),
            ("no leftovers from an earlier run", self._check_leftovers),
            ("docker works and the image is local", self._check_docker),
            ("this machine's public IP (for the /32 ingress rule)", self._check_ip),
        ]
        for name, check in checks:
            try:
                with self.step(name) as s:
                    s.note = check()
            except (ClientError, BotoCoreError, RuntimeError, WaiterError, OSError):
                ok = False
        return ok

    def _check_identity(self) -> str:
        ident = self.client("sts").get_caller_identity()
        if ident["Account"] != self.account:
            raise RuntimeError(f"session is in account {ident['Account']}, not {self.account}")
        if ":assumed-role/groundcrew-deploy/" not in ident["Arn"]:
            raise RuntimeError(f"not the deploy role: {ident['Arn']}")
        return str(ident["Arn"])

    def _check_cluster(self) -> str:
        clusters = self.client("ecs").describe_clusters(clusters=[self.cluster])["clusters"]
        if not clusters or clusters[0].get("status") != "ACTIVE":
            raise RuntimeError(f"{self.cluster} is not ACTIVE (is the stack Tier=deploy?)")
        return str(clusters[0]["clusterArn"])

    def _check_subnets(self) -> str:
        self.subnets = resolve_subnets(self.deploy, self.settings)
        subnet = self.client("ec2").describe_subnets(SubnetIds=self.subnets[:1])["Subnets"][0]
        self.vpc_id = str(subnet["VpcId"])
        return f"{self.vpc_id}: {', '.join(self.subnets)}"

    def _check_slr(self) -> str:
        role = self.read.client("iam").get_role(RoleName="AWSServiceRoleForECS")["Role"]
        return str(role["Arn"])

    def _check_leftovers(self) -> str:
        found = self.leftovers()
        if found:
            raise RuntimeError(
                f"left over from an earlier run: {', '.join(found)}. "
                "Run make smoke-ecs CLEANUP=1 first."
            )
        return "none"

    def _check_docker(self) -> str:
        self._docker("info", "--format", "{{.ServerVersion}}")
        self._docker("pull", "--quiet", "--platform", "linux/amd64", SOURCE_IMAGE)
        return SOURCE_IMAGE

    def _check_ip(self) -> str:
        return f"{self._public_ip()}/32"

    def leftovers(self) -> list[str]:
        """Smoke resources that exist now (read-only)."""
        found = []
        try:
            self.client("ecr").describe_repositories(repositoryNames=[REPO])
            found.append(f"ECR {REPO}")
        except ClientError as exc:
            if error_code(exc) != "RepositoryNotFoundException":
                raise
        groups = self.client("logs").describe_log_groups(logGroupNamePrefix=LOG_GROUP)
        if any(g["logGroupName"] == LOG_GROUP for g in groups.get("logGroups", [])):
            found.append(f"log group {LOG_GROUP}")
        try:
            described = self.client("secretsmanager").describe_secret(SecretId=SECRET)
            # A force-deleted secret stays describable (with DeletedDate) for up to a minute.
            if not described.get("DeletedDate"):
                found.append(f"secret {SECRET}")
        except ClientError as exc:
            if error_code(exc) != "ResourceNotFoundException":
                raise
        services = self.client("ecs").describe_services(cluster=self.cluster, services=[SERVICE])
        if any(s.get("status") != "INACTIVE" for s in services.get("services", [])):
            found.append(f"service {SERVICE}")
        groups = self.client("ec2").describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [f"{SG_PREFIX}*"]}]
        )["SecurityGroups"]
        found += [f"security group {g['GroupId']} ({g['GroupName']})" for g in groups]
        return found

    def plan(self) -> list[str]:
        """Every AWS write the real run makes, in order (printed by the dry run)."""
        tags = f"tags {PROJECT_TAG}={PROJECT_VALUE}, {DEPLOY_TAG}={self.run_id}"
        role = task_execution_role_arn(self.settings, self.account)
        return [
            f"ecr:CreateRepository {REPO} ({tags}); docker push {SOURCE_IMAGE} as {REPO}:<tag>",
            f"logs:CreateLogGroup {LOG_GROUP} ({tags}); logs:PutRetentionPolicy 1 day",
            f"ec2:CreateSecurityGroup {SG_PREFIX}<time> in {self.vpc_id or 'the default VPC'} "
            f"({tags}); ec2:AuthorizeSecurityGroupIngress tcp/80 from <this IP>/32",
            f"secretsmanager:CreateSecret {SECRET} (random dummy value, {tags})",
            f"ecs:RegisterTaskDefinition {FAMILY} (0.25 vCPU / 512 MB, execution role {role}, "
            f"secret TEST_VAR, {tags})",
            f"ecs:CreateService {SERVICE} in {self.cluster} (public IP, {tags}); wait stable; "
            "curl the task IP; read the TEST_VAR hash from the log",
            "negative checks a-e (AWS must refuse; c uses DryRun)",
            f"cleanup: service {SERVICE}, the security group, {LOG_GROUP}, {REPO} (force), "
            f"{SECRET} (no recovery); the task definition revision stays",
        ]

    # --- the real run ------------------------------------------------------------------

    def run(self) -> None:
        image = ""
        try:
            with self.step("1 ECR repository and image push") as s:
                image = self._push_image()
                s.note = image
            with self.step("2 log group, 1-day retention") as s:
                self._log_group()
                s.note = LOG_GROUP
            with self.step("3 security group, tcp/80 from this IP only") as s:
                s.note = self._security_group()
            with self.step("4 secret (dummy value)") as s:
                self._secret()
                s.note = SECRET
            with self.step("5 task definition") as s:
                s.note = self._task_definition(image)
            with self.step("6 service stable, task IP") as s:
                s.note = self._service()
            ip = s.note.split()[-1]
            with self.step("7 HTTP 200 from the task") as s:
                s.note = self._http(ip)
            with self.step("8 TEST_VAR injected (hash only)") as s:
                s.note = self._secret_seen()
        except (ClientError, BotoCoreError, RuntimeError, WaiterError, OSError):
            pass  # recorded by step(); the negative checks and cleanup still run
        finally:
            self._negatives()
            self.cleanup()

    def _push_image(self) -> str:
        ecr = self.client("ecr")
        ecr.create_repository(repositoryName=REPO, tags=self.tags)
        self.created.repository = True
        auth = ecr.get_authorization_token()["authorizationData"][0]
        self.scrub.register(str(auth["authorizationToken"]))
        user, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
        self.scrub.register(password)
        registry = str(auth["proxyEndpoint"]).removeprefix("https://")
        image = f"{registry}/{REPO}:{self.run_id}"
        with tempfile.TemporaryDirectory(prefix="groundcrew-docker-") as config:
            env = {"DOCKER_CONFIG": config}
            self._docker("login", "--username", user, "--password-stdin", registry,
                         stdin=password, env=env)  # fmt: skip
            self._docker("tag", SOURCE_IMAGE, image, env=env)
            self._docker("push", "--quiet", image, env=env)
        return image

    def _log_group(self) -> None:
        logs = self.client("logs")
        logs.create_log_group(
            logGroupName=LOG_GROUP, tags={t["Key"]: t["Value"] for t in self.tags}
        )
        self.created.log_group = True
        logs.put_retention_policy(logGroupName=LOG_GROUP, retentionInDays=1)

    def _security_group(self) -> str:
        ec2 = self.client("ec2")
        ip = self._public_ip()
        group = ec2.create_security_group(
            GroupName=f"{SG_PREFIX}{self.run_id.removeprefix('smoke-')}",
            Description="Groundcrew smoke test (deleted by the test)",
            VpcId=self.vpc_id,
            TagSpecifications=[{"ResourceType": "security-group", "Tags": self.tags}],
        )
        self.created.security_group = str(group["GroupId"])
        ec2.authorize_security_group_ingress(
            GroupId=self.created.security_group,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 80,
                    "ToPort": 80,
                    "IpRanges": [{"CidrIp": f"{ip}/32", "Description": "Groundcrew smoke"}],
                }
            ],
        )
        return f"{self.created.security_group}, tcp/80 from {ip}/32"

    def _secret(self) -> None:
        self._secret_value = secrets.token_urlsafe(24)
        self.scrub.register(self._secret_value)
        made = self.client("secretsmanager").create_secret(
            Name=SECRET,
            Description="Groundcrew smoke test dummy value (deleted by the test)",
            SecretString=self._secret_value,
            Tags=self.tags,
        )
        self.created.secret_arn = str(made["ARN"])
        self._secret_created = time.monotonic()

    def secret_hash(self) -> str:
        return hashlib.sha256(self._secret_value.encode()).hexdigest()[:HASH_CHARS]

    def _task_definition(self, image: str) -> str:
        made = self.client("ecs").register_task_definition(
            family=FAMILY,
            requiresCompatibilities=["FARGATE"],
            networkMode="awsvpc",
            cpu="256",
            memory="512",
            runtimePlatform={"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
            executionRoleArn=task_execution_role_arn(self.settings, self.account),
            containerDefinitions=[
                {
                    "name": CONTAINER,
                    "image": image,
                    "essential": True,
                    "entryPoint": ["sh", "-c"],
                    "command": [CONTAINER_SCRIPT],
                    "portMappings": [{"containerPort": 80, "protocol": "tcp"}],
                    "secrets": [{"name": "TEST_VAR", "valueFrom": self.created.secret_arn}],
                    "logConfiguration": {
                        "logDriver": "awslogs",
                        "options": {
                            "awslogs-group": LOG_GROUP,
                            "awslogs-region": self.region,
                            "awslogs-stream-prefix": SERVICE,
                        },
                    },
                }
            ],
            tags=[{"key": t["Key"], "value": t["Value"]} for t in self.tags],
        )
        arn = str(made["taskDefinition"]["taskDefinitionArn"])
        self.created.task_definition = arn
        return arn.rsplit("/", 1)[-1]

    def _service(self) -> str:
        ecs = self.client("ecs")
        ecs.create_service(
            cluster=self.cluster,
            serviceName=SERVICE,
            taskDefinition=str(self.created.task_definition),
            desiredCount=1,
            launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": self.subnets,
                    "securityGroups": [str(self.created.security_group)],
                    "assignPublicIp": "ENABLED",
                }
            },
            propagateTags="SERVICE",
            tags=[{"key": t["Key"], "value": t["Value"]} for t in self.tags],
        )
        self.created.service = True
        self._service_started = time.monotonic()
        try:
            ecs.get_waiter("services_stable").wait(
                cluster=self.cluster,
                services=[SERVICE],
                WaiterConfig={"Delay": 10, "MaxAttempts": STABLE_TIMEOUT_SECONDS // 10},
            )
        except WaiterError:
            raise RuntimeError(f"service not stable in {STABLE_TIMEOUT_SECONDS} s: "
                               f"{self._stopped_reasons()}") from None  # fmt: skip
        arns = ecs.list_tasks(cluster=self.cluster, serviceName=SERVICE)["taskArns"]
        task = ecs.describe_tasks(cluster=self.cluster, tasks=arns[:1])["tasks"][0]
        eni = next(
            d["value"]
            for a in task.get("attachments", [])
            for d in a.get("details", [])
            if d["name"] == "networkInterfaceId"
        )
        nic = self.client("ec2").describe_network_interfaces(NetworkInterfaceIds=[eni])
        ip = nic["NetworkInterfaces"][0].get("Association", {}).get("PublicIp")
        if not ip:
            raise RuntimeError(f"task {task['taskArn']} has no public IP")
        return f"task {str(task['taskArn']).rsplit('/', 1)[-1]} at {ip}"

    def _stopped_reasons(self) -> str:
        ecs = self.client("ecs")
        arns = ecs.list_tasks(cluster=self.cluster, serviceName=SERVICE, desiredStatus="STOPPED")
        if not arns["taskArns"]:
            return "no stopped tasks"
        tasks = ecs.describe_tasks(cluster=self.cluster, tasks=arns["taskArns"][:3])["tasks"]
        return "; ".join(str(t.get("stoppedReason", "?")) for t in tasks)

    def _http(self, ip: str) -> str:
        deadline = time.monotonic() + HTTP_TIMEOUT_SECONDS
        last = ""
        while time.monotonic() < deadline:
            try:
                status = self._http_get(f"http://{ip}/")
                if status == 200:
                    return f"http://{ip}/ returned 200"
                last = f"status {status}"
            except OSError as exc:
                last = type(exc).__name__
            time.sleep(5)
        raise RuntimeError(f"no 200 from http://{ip}/ in {HTTP_TIMEOUT_SECONDS} s ({last})")

    def _secret_seen(self) -> str:
        logs = self.client("logs")
        deadline = time.monotonic() + LOG_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            events = logs.filter_log_events(logGroupName=LOG_GROUP, filterPattern='"TEST_VAR="')
            for event in events.get("events", []):
                line = str(event["message"]).strip()
                if line == f"TEST_VAR=set sha256:{self.secret_hash()}":
                    return f"container saw TEST_VAR, sha256 prefix {self.secret_hash()} matches"
                if line.startswith("TEST_VAR="):
                    raise RuntimeError(f"container logged {self.scrub.clean(line)!r}")
            time.sleep(5)
        raise RuntimeError(f"no TEST_VAR line in {LOG_GROUP} in {LOG_TIMEOUT_SECONDS} s")

    def _negatives(self) -> None:
        """Run the five hardcoded checks; for a and e also show the code allowlist refuses."""
        guarded = []
        guard_checks: list[tuple[str, Callable[[], Any]]] = [
            ("a", lambda: self.client("secretsmanager").get_secret_value(SecretId=SECRET)),
            ("e", lambda: self.client("iam").create_role(
                RoleName="groundcrew-smoke-should-not-exist", AssumeRolePolicyDocument="{}")),
        ]  # fmt: skip
        for name, call in guard_checks:
            try:
                call()
                guarded.append(f"{name}: code allowlist let it through")
            except ReadOnlyViolationError:
                guarded.append(f"{name}: code allowlist refused")
            except (ClientError, BotoCoreError) as exc:  # the guard let it reach AWS
                guarded.append(f"{name}: code allowlist let it through ({error_code(exc)})")
        started = time.monotonic()
        try:
            outcomes = negative_checks(
                self.deploy,
                self.region,
                self.vpc_id,
                self.created.task_definition or FAMILY,
                self.tags,
            )
        except (ClientError, BotoCoreError, RuntimeError) as exc:
            self.report.steps.append(Step("9 negative checks", "FAIL", 0.0, error_code(exc)))
            return
        per = (time.monotonic() - started) / max(len(outcomes), 1)
        for name, outcome in outcomes:
            verdict = negative_verdict(name, outcome)
            note = f"AWS: {outcome}"
            if name.startswith(("a ", "e ")):
                note += f"; {guarded[0] if name.startswith('a ') else guarded[1]}"
            step = Step(f"9{name}", verdict, per, note)
            self.report.steps.append(step)
            self.scrub.say(f"  {verdict:<4} 9{name} {note}")
            if verdict == "GAP":
                self.report.gaps.append(f"{name}: {outcome}")
                command = NEGATIVE_CLEANUP.get(name, "")
                if command and outcome == "allowed":
                    self.report.manual_cleanup.append(command.format(region=self.region))

    # --- cleanup -----------------------------------------------------------------------

    def cleanup(self, *, leftovers_only: bool = False) -> None:
        """Delete what this run created, in dependency order, or (leftovers_only) the fixed
        smoke names when they are tagged project=groundcrew. Each delete checks ownership
        first: the resource's own tags where AWS returns them (service, security group,
        secret), otherwise this run's creation record or, for leftovers, tag:GetResources.
        Every delete is tried; failures are listed in report.leftovers."""
        self._leftovers_only = leftovers_only
        targets: list[tuple[str, bool, Callable[[], str]]] = [
            ("service", self.created.service, self._delete_service),
            ("security group", bool(self.created.security_group), self._delete_security_groups),
            ("log group", self.created.log_group, self._delete_log_group),
            ("ECR repository", self.created.repository, self._delete_repository),
            ("secret", bool(self.created.secret_arn), self._delete_secret),
        ]
        for name, created, delete in targets:
            if not (created or leftovers_only):
                continue
            started = time.monotonic()
            try:
                note, result = delete(), "PASS"
            except NotOursError as exc:
                note, result = str(exc), "SKIP"
            except (ClientError, BotoCoreError, RuntimeError, WaiterError) as exc:
                note, result = self.scrub.clean(f"{error_code(exc)}: {exc}")[:200], "FAIL"
                self.report.leftovers.append(f"{name}: {note}")
            step = Step(f"10 delete {name}", result, time.monotonic() - started, note)
            self.report.steps.append(step)
            self.scrub.say(f"  {result:<4} {step.name} ({step.seconds:.1f} s) {note}")
        if self._service_started:
            self.report.service_seconds = time.monotonic() - self._service_started
        secret_seconds = time.monotonic() - self._secret_created if self._secret_created else 0
        self.report.estimated_cost_usd = estimate_cost(self.report.service_seconds, secret_seconds)

    def owns(self, tags: dict[str, str]) -> bool:
        """This run's resource (both tags), or for leftovers any project=groundcrew one."""
        if tags.get(PROJECT_TAG) != PROJECT_VALUE:
            return False
        return self._leftovers_only or tags.get(DEPLOY_TAG) == self.run_id

    def _project_tagged(self) -> set[str]:
        """ARNs tagged project=groundcrew (tag:GetResources), without a trailing ':*'."""
        pages = self.client("resourcegroupstaggingapi").get_paginator("get_resources")
        arns: set[str] = set()
        for page in pages.paginate(TagFilters=[{"Key": PROJECT_TAG, "Values": [PROJECT_VALUE]}]):
            arns |= {
                str(r["ResourceARN"]).removesuffix(":*")
                for r in page.get("ResourceTagMappingList", [])
            }
        return arns

    def _arn(self, kind: str) -> str:
        base = f"arn:aws:{kind}:{self.region}:{self.account}"
        return f"{base}:log-group:{LOG_GROUP}" if kind == "logs" else f"{base}:repository/{REPO}"

    def _delete_service(self) -> str:
        ecs = self.client("ecs")
        found = ecs.describe_services(cluster=self.cluster, services=[SERVICE], include=["TAGS"])
        live = [s for s in found.get("services", []) if s.get("status") != "INACTIVE"]
        if not live:
            return "none found"
        if not self.owns({t["key"]: t["value"] for t in live[0].get("tags", [])}):
            raise NotOursError(f"service {SERVICE} is not tagged by this run; not deleted")
        ecs.update_service(cluster=self.cluster, service=SERVICE, desiredCount=0)
        ecs.delete_service(cluster=self.cluster, service=SERVICE, force=True)
        ecs.get_waiter("services_inactive").wait(
            cluster=self.cluster, services=[SERVICE], WaiterConfig={"Delay": 10, "MaxAttempts": 30}
        )
        return f"{SERVICE} inactive"

    def _delete_security_groups(self) -> str:
        """Smoke groups whose own tags say they are ours (EC2 Describe returns tags)."""
        ec2 = self.client("ec2")
        groups = ec2.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [f"{SG_PREFIX}*"]}]
        )["SecurityGroups"]
        mine = [g for g in groups if self.owns({t["Key"]: t["Value"] for t in g.get("Tags", [])})]
        deleted = []
        for group in mine:
            deadline = time.monotonic() + SG_DELETE_TIMEOUT_SECONDS
            while True:
                try:
                    ec2.delete_security_group(GroupId=group["GroupId"])
                    deleted.append(str(group["GroupId"]))
                    break
                except ClientError as exc:
                    # The task's network interface takes a while to be released.
                    if error_code(exc) != "DependencyViolation" or time.monotonic() > deadline:
                        raise
                    time.sleep(10)
        skipped = len(groups) - len(mine)
        note = ", ".join(deleted) or "none found"
        return f"{note} ({skipped} not ours, left alone)" if skipped else note

    def _owned_without_tag_read(self, kind: str, created: bool) -> bool:
        """ECR repositories and log groups: the deploy role cannot read their tags directly.
        This run: ours if this run created it. Leftovers: ours if tag:GetResources lists it
        with project=groundcrew."""
        if not self._leftovers_only:
            return created
        return self._arn(kind) in self._project_tagged()

    def _delete_log_group(self) -> str:
        if not self._owned_without_tag_read("logs", self.created.log_group):
            raise NotOursError(f"{LOG_GROUP}: none tagged project=groundcrew")
        try:
            self.client("logs").delete_log_group(logGroupName=LOG_GROUP)
        except ClientError as exc:
            if error_code(exc) == "ResourceNotFoundException":
                return "none found"
            raise
        return LOG_GROUP

    def _delete_repository(self) -> str:
        if not self._owned_without_tag_read("ecr", self.created.repository):
            raise NotOursError(f"{REPO}: none tagged project=groundcrew")
        try:
            self.client("ecr").delete_repository(repositoryName=REPO, force=True)
        except ClientError as exc:
            if error_code(exc) == "RepositoryNotFoundException":
                return "none found"
            raise
        return f"{REPO} (force)"

    def _delete_secret(self) -> str:
        sm = self.client("secretsmanager")
        try:
            described = sm.describe_secret(SecretId=SECRET)
        except ClientError as exc:
            if error_code(exc) == "ResourceNotFoundException":
                return "none found"
            raise
        if not self.owns({t["Key"]: t["Value"] for t in described.get("Tags", [])}):
            raise NotOursError(f"{SECRET} is not tagged by this run; not deleted")
        sm.delete_secret(SecretId=SECRET, ForceDeleteWithoutRecovery=True)
        # Deletion is asynchronous: wait until it is really gone, so the next run starts clean.
        deadline = time.monotonic() + SECRET_GONE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                sm.describe_secret(SecretId=SECRET)
            except ClientError as exc:
                if error_code(exc) == "ResourceNotFoundException":
                    return f"{SECRET} (no recovery, gone)"
                raise
            time.sleep(5)
        return f"{SECRET} (no recovery; still being removed by AWS)"


# ------------------------------------------------------------------------------ helpers


def _docker(
    *args: str, stdin: str | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    if not shutil.which("docker"):
        raise RuntimeError("docker is not installed")
    result = subprocess.run(  # noqa: S603 (fixed docker commands)
        ["docker", *args],  # noqa: S607
        input=stdin,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        env={**os.environ, **(env or {})},
    )
    if result.returncode != 0:
        # The caller's Scrubber cleans this before it is printed (it may echo the registry).
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr.strip()[-300:]}")
    return result


def _public_ip() -> str:
    with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10) as r:
        return str(r.read().decode()).strip()


def _http_get(url: str) -> int:
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 (the smoke task's own IP)
        return int(r.status)


def write_report(report: Report, scrub: Scrubber, directory: Path) -> Path:
    directory.mkdir(exist_ok=True)
    path = directory / f"{report.run_id}.json"
    data = asdict(report)
    data["ok"] = report.ok
    path.write_text(scrub.check(json.dumps(data, indent=2)) + "\n")
    return path


async def _sessions(connection_id: uuid.UUID) -> tuple[boto3.Session, boto3.Session, str]:
    from app.connectors.credentials import ConnectionCredentialsProvider
    from app.db.models import Connection
    from app.db.session import get_session_factory

    async with get_session_factory()() as db:
        conn = await db.get(Connection, connection_id)
    if conn is None or conn.kind != "aws":
        raise SystemExit(f"error: no AWS connection {connection_id}")
    provider = ConnectionCredentialsProvider([conn])
    deploy = await asyncio.to_thread(provider.aws_deploy_session, conn.id)
    read = await asyncio.to_thread(provider.aws_session, conn.id)
    return deploy, read, str(conn.config.get("account_id") or "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--connection", required=True, type=uuid.UUID)
    parser.add_argument("--real", action="store_true", help="make AWS writes (DRY_RUN=0)")
    parser.add_argument("--cleanup", action="store_true", help="remove smoke leftovers")
    args = parser.parse_args(argv)
    os.chdir(REPO_ROOT)  # Settings reads .env from here
    scrub = Scrubber()
    run_id = "smoke-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    try:
        deploy, read, account = asyncio.run(_sessions(args.connection))
    except Exception as exc:  # one clear line, never a traceback
        scrub.say(f"error: could not get the connection's sessions: {scrub.clean(str(exc))}")
        return 1
    dry_run = not (args.real or args.cleanup)
    smoke = Smoke(deploy, read, get_settings(), account=account, run_id=run_id,
                  dry_run=dry_run, scrub=scrub)  # fmt: skip
    mode = "CLEANUP" if args.cleanup else "DRY RUN (read-only)" if dry_run else "REAL RUN"
    scrub.say(f"{mode} {run_id}: account {account}, {smoke.region}, cluster {smoke.cluster}")
    if args.cleanup:
        smoke.cleanup(leftovers_only=True)
    else:
        scrub.say("Read-only checks:")
        ready = smoke.prechecks()
        if dry_run or not ready:
            scrub.say("\nThe real run (DRY_RUN=0) would make these AWS writes, in order:")
            for i, line in enumerate(smoke.plan(), 1):
                scrub.say(f"  {i}. {line}")
            if not ready:
                scrub.say("\nNot ready: fix the failed checks above first.")
        else:
            scrub.say("Real run:")
            smoke.run()
    report = smoke.report
    scrub.say("\n" + table(report))
    if not dry_run:
        cost = f"${report.estimated_cost_usd:.4f}"
        scrub.say(f"\nService ran {report.service_seconds:.0f} s; estimated cost {cost} "
                  "(us-east-2 list prices, estimate)")  # fmt: skip
    for gap in report.gaps:
        scrub.say(f"IAM GAP: {gap}")
    for command in report.manual_cleanup:
        scrub.say(f"Run this yourself (the deploy role cannot): {command}")
    for leftover in report.leftovers:
        scrub.say(f"NOT CLEANED UP: {leftover}")
    path = write_report(report, scrub, REPO_ROOT / ".smoke")
    scrub.say(f"Report: {path.relative_to(REPO_ROOT)}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
