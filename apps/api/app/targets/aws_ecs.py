"""AWS ECS (Fargate) target, with the connection's groundcrew-deploy role only.

Apply order, the same as `scripts/smoke_ecs_deploy.py` (docs/deploy-live.md):

    ECR repository -> push image -> log group /groundcrew/<app> -> security group
    -> Secrets Manager secrets -> task definition (new revision) -> service in groundcrew-deploys

Every AWS call uses `CredentialsProvider.aws_deploy_session()` (guarded by
AWS_DEPLOY_OPERATIONS); names come from app.connectors.deploy_infra and every created
resource carries its tags. Task definitions are never deregistered: every revision stays
available for a rollback. The security group allows the app's port from GC_INGRESS_CIDR
(0.0.0.0/0 for the demo).

Rollback points the service back at the previous task definition; rolling back a first
deploy deletes the service (the image, log group, security group, and secrets stay for
`make cleanup-deploys`). Both need an approved `target_rollback` (emergency) action.
"""

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

import boto3
import httpx
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings
from app.connectors.credentials import CredentialsProvider
from app.connectors.deploy_infra import (
    aws_tags,
    ecr_repository,
    ecs_service,
    log_group,
    required_tags,
    resolve_subnets,
    secret_name,
    slug,
    task_execution_role_arn,
    task_family,
)
from app.db.session import SessionFactory, get_session_factory
from app.discovery.types import ConnectorResult, DiscoveredResource
from app.pipeline.events import PreflightCheck, TargetKind, VerifyCheck
from app.policy.actions import (
    TARGET_APPLY,
    TARGET_ROLLBACK,
    ApprovedAction,
    consume_approved_action,
)
from app.sandbox.base import Sandbox
from app.sandbox.registry import ecr_target
from app.security.redact import redact
from app.targets.base import (
    ApplyStep,
    Deployment,
    DeploySpec,
    Health,
    LogLine,
    TargetError,
    TargetPlan,
    check_spec_matches,
    common_checks,
    env_views,
    secret_values,
)

CPU, MEMORY = "256", "512"
LOG_TAIL_LINES = 50


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


class EcsTarget:
    kind: TargetKind = "aws_ecs"

    def __init__(
        self,
        settings: Settings,
        credentials: CredentialsProvider,
        connection_id: uuid.UUID,
        *,
        session: boto3.Session | None = None,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self.settings = settings
        self.credentials = credentials
        self.connection_id = connection_id
        self._session = session
        self._http = http
        self._sleep = sleep
        self._session_factory = session_factory
        self._account: str | None = None

    # --- plumbing -------------------------------------------------------------------------

    @property
    def session_factory(self) -> SessionFactory:
        """Where approved actions are spent (approvals.used_at)."""
        if self._session_factory is None:
            self._session_factory = get_session_factory()
        return self._session_factory

    @property
    def session(self) -> boto3.Session:
        if self._session is None:
            self._session = self.credentials.aws_deploy_session(self.connection_id)
        return self._session

    @property
    def region(self) -> str:
        return self.settings.gc_aws_region

    @property
    def cluster(self) -> str:
        return self.settings.gc_ecs_cluster

    async def _aws(self, api: str, operation: str, /, **kwargs: Any) -> dict[str, Any]:
        """One AWS call in a thread. ClientError is raised as is for the caller to map; other
        botocore errors become TargetError. Never logs the request: a secret value is only
        ever in a request."""

        def call() -> dict[str, Any]:
            client: Any = self.session.client(api, region_name=self.region)
            return dict(getattr(client, operation)(**kwargs))

        try:
            return await asyncio.to_thread(call)
        except ClientError:
            raise
        except BotoCoreError as exc:
            raise TargetError(f"{api}:{operation} failed: {type(exc).__name__}") from None

    def _error(self, what: str, exc: ClientError) -> TargetError:
        message = str(exc.response.get("Error", {}).get("Message", ""))[:300]
        return TargetError(f"{what} failed: {_code(exc)} {redact(message)}".strip())

    async def _account_id(self) -> str:
        if self._account is None:
            self._account = str((await self._aws("sts", "get_caller_identity"))["Account"])
        return self._account

    def _names(self, app: str) -> dict[str, str]:
        name = slug(app)
        return {
            "app": name,
            "repository": ecr_repository(self.settings, app),
            "log_group": log_group(self.settings, app),
            "security_group": f"groundcrew-{name}",
            "family": task_family(app),
            "service": ecs_service(app),
        }

    async def _vpc(self) -> tuple[list[str], str]:
        subnets = await asyncio.to_thread(resolve_subnets, self.session, self.settings)
        found = await self._aws("ec2", "describe_subnets", SubnetIds=subnets[:1])
        return subnets, str(found["Subnets"][0]["VpcId"])

    # --- what exists ----------------------------------------------------------------------

    async def _repository_exists(self, name: str) -> bool:
        try:
            await self._aws("ecr", "describe_repositories", repositoryNames=[name])
            return True
        except ClientError as exc:
            if _code(exc) == "RepositoryNotFoundException":
                return False
            raise self._error("ecr:DescribeRepositories", exc) from None

    async def _log_group_exists(self, name: str) -> bool:
        found = await self._aws("logs", "describe_log_groups", logGroupNamePrefix=name)
        return any(g.get("logGroupName") == name for g in found.get("logGroups", []))

    async def _security_group(self, name: str, vpc_id: str) -> str | None:
        found = await self._aws(
            "ec2",
            "describe_security_groups",
            Filters=[
                {"Name": "group-name", "Values": [name]},
                {"Name": "vpc-id", "Values": [vpc_id]},
            ],
        )
        groups = found.get("SecurityGroups", [])
        return str(groups[0]["GroupId"]) if groups else None

    async def _secret_arn(self, name: str) -> str | None:
        try:
            return str((await self._aws("secretsmanager", "describe_secret", SecretId=name))["ARN"])
        except ClientError as exc:
            if _code(exc) == "ResourceNotFoundException":
                return None
            raise self._error("secretsmanager:DescribeSecret", exc) from None

    async def _service(self, name: str) -> dict[str, Any] | None:
        found = await self._aws(
            "ecs", "describe_services", cluster=self.cluster, services=[name], include=["TAGS"]
        )
        for service in found.get("services", []):
            if service.get("status") == "ACTIVE":
                return dict(service)
        return None

    # --- discover -------------------------------------------------------------------------

    async def discover(self) -> ConnectorResult:
        """The services in the groundcrew-deploys cluster, keyed by ARN like AWS discovery."""
        account = await self._account_id()
        arns: list[str] = []
        token: str | None = None
        while True:
            page = await self._aws(
                "ecs",
                "list_services",
                cluster=self.cluster,
                **({"nextToken": token} if token else {}),
            )
            arns += page.get("serviceArns", [])
            token = page.get("nextToken")
            if not token:
                break
        resources = []
        for start in range(0, len(arns), 10):
            found = await self._aws(
                "ecs",
                "describe_services",
                cluster=self.cluster,
                services=arns[start : start + 10],
                include=["TAGS"],
            )
            for s in found.get("services", []):
                resources.append(
                    DiscoveredResource(
                        provider="aws",
                        type="ecs:service",
                        external_id=str(s["serviceArn"]),
                        name=str(s["serviceName"]),
                        account=account,
                        region=self.region,
                        tags={str(t["key"]): str(t["value"]) for t in s.get("tags", [])},
                        config={
                            "cluster": self.cluster,
                            "task_definition": s.get("taskDefinition"),
                            "desired": s.get("desiredCount"),
                            "running": s.get("runningCount"),
                            "launch_type": s.get("launchType"),
                        },
                    )
                )
        return ConnectorResult(resources=resources)

    # --- plan -----------------------------------------------------------------------------

    def _task_definition(
        self, plan: TargetPlan, account: str, secret_arns: dict[str, str] | None
    ) -> dict[str, Any]:
        """The task definition apply registers. Secrets reference Secrets Manager (ARNs at
        apply, names in the plan); their values are never in it."""
        n = self._names(plan.app)
        secrets = [v.name for v in plan.env if v.secret and v.provided]
        return {
            "family": n["family"],
            "requiresCompatibilities": ["FARGATE"],
            "networkMode": "awsvpc",
            "cpu": plan.cpu,
            "memory": plan.memory,
            "runtimePlatform": {"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
            "executionRoleArn": task_execution_role_arn(self.settings, account),
            "containerDefinitions": [
                {
                    "name": n["app"],
                    "image": plan.image,
                    "essential": True,
                    "portMappings": [{"containerPort": plan.port, "protocol": "tcp"}],
                    "environment": [
                        {"name": v.name, "value": v.value or ""}
                        for v in plan.env
                        if v.provided and not v.secret
                    ],
                    "secrets": [
                        {
                            "name": name,
                            "valueFrom": (secret_arns or {}).get(name)
                            or secret_name(plan.app, name),
                        }
                        for name in secrets
                    ],
                    "logConfiguration": {
                        "logDriver": "awslogs",
                        "options": {
                            "awslogs-group": n["log_group"],
                            "awslogs-region": self.region,
                            "awslogs-stream-prefix": n["app"],
                        },
                    },
                }
            ],
        }

    async def plan(self, spec: DeploySpec) -> TargetPlan:
        n = self._names(spec.app)
        account = await self._account_id()
        image = f"{account}.dkr.ecr.{self.region}.amazonaws.com/{n['repository']}:{spec.tag}"
        _, vpc_id = await self._vpc()
        service = await self._service(n["service"])
        plan = TargetPlan(
            target="aws_ecs",
            connection_id=spec.connection_id,
            run_id=spec.run_id,
            app=n["app"],
            image=image,
            source_image_id=spec.source_image_id,
            image_tested=spec.smoke_passed,
            port=spec.port,
            health_path=spec.health_path,
            replicas=spec.replicas,
            cpu=CPU,
            memory=MEMORY,
            env=env_views(spec),
            allow_missing_env=spec.allow_missing_env,
            steps=[],
            rendered="",
            rendered_format="json",
            previous_revision=str(service["taskDefinition"]) if service else None,
        )
        cidr = self.settings.gc_ingress_cidr

        def create(exists: bool) -> Literal["create", "unchanged"]:
            return "unchanged" if exists else "create"

        steps = [
            ApplyStep(
                kind="ecr:Repository",
                name=n["repository"],
                action=create(await self._repository_exists(n["repository"])),
            ),
            ApplyStep(
                kind="image",
                name=image,
                action="push",
                detail=f"{spec.source_image_id[:19]} from the sandbox",
            ),
            ApplyStep(
                kind="logs:LogGroup",
                name=n["log_group"],
                action=create(await self._log_group_exists(n["log_group"])),
                detail=f"{self.settings.gc_log_retention_days}-day retention",
            ),
            ApplyStep(
                kind="ec2:SecurityGroup",
                name=n["security_group"],
                action=create(await self._security_group(n["security_group"], vpc_id) is not None),
                detail=f"tcp/{spec.port} from {cidr}",
            ),
        ]
        for name in secret_values(spec):
            exists = await self._secret_arn(secret_name(spec.app, name)) is not None
            steps.append(
                ApplyStep(
                    kind="secretsmanager:Secret",
                    name=secret_name(spec.app, name),
                    action="update" if exists else "create",
                )
            )
        steps += [
            ApplyStep(
                kind="ecs:TaskDefinition",
                name=n["family"],
                action="create",
                detail="new revision, 0.25 vCPU / 0.5 GB",
            ),
            ApplyStep(
                kind="ecs:Service",
                name=n["service"],
                action="update" if service else "create",
                detail=f"{self.cluster}, {spec.replicas} task, public IP",
            ),
        ]
        plan.steps = steps
        plan.rendered = json.dumps(
            {
                "taskDefinition": self._task_definition(plan, account, None),
                "service": {
                    "cluster": self.cluster,
                    "serviceName": n["service"],
                    "desiredCount": spec.replicas,
                    "launchType": "FARGATE",
                    "assignPublicIp": "ENABLED",
                    "securityGroup": {
                        "name": n["security_group"],
                        "ingress": f"tcp/{spec.port} from {cidr}",
                    },
                },
                "tags": required_tags(spec.run_id),
            },
            indent=2,
        )
        return plan

    async def preflight(self, plan: TargetPlan) -> list[PreflightCheck]:
        """Read-only: the plan's own checks, the cluster, and whether the service is ours."""
        checks = common_checks(plan)
        n = self._names(plan.app)
        clusters = await self._aws("ecs", "describe_clusters", clusters=[self.cluster])
        active = any(c.get("status") == "ACTIVE" for c in clusters.get("clusters", []))
        checks.append(
            PreflightCheck(
                name="other",
                status="pass" if active else "fail",
                detail=f"Cluster {self.cluster} is active"
                if active
                else f"Cluster {self.cluster} does not exist: update the stack with Tier=deploy",
            )
        )
        service = await self._service(n["service"])
        tags = {str(t["key"]): str(t["value"]) for t in (service or {}).get("tags", [])}
        if service and tags.get("project") != "groundcrew":
            checks.append(
                PreflightCheck(
                    name="name_collision",
                    status="fail",
                    detail=f"Service {n['service']} exists and was not created by Groundcrew",
                )
            )
        else:
            checks.append(
                PreflightCheck(
                    name="name_collision",
                    status="pass",
                    detail=f"Updates Groundcrew's {n['service']}"
                    if service
                    else f"No existing {n['service']}",
                )
            )
        return checks

    # --- apply ----------------------------------------------------------------------------

    async def apply(
        self, plan: TargetPlan, spec: DeploySpec, token: ApprovedAction, sandbox: Sandbox
    ) -> Deployment:
        await consume_approved_action(
            self.session_factory,
            token,
            run_id=plan.run_id,
            action=TARGET_APPLY,
            plan_hash=plan.plan_hash(),
        )
        check_spec_matches(plan, spec)
        n = self._names(plan.app)
        account = await self._account_id()
        tags = aws_tags(plan.run_id)
        tag_map = required_tags(plan.run_id)
        subnets, vpc_id = await self._vpc()
        try:
            # 1. ECR repository, then the push: nothing left the sandbox before approval.
            if not await self._repository_exists(n["repository"]):
                try:
                    await self._aws(
                        "ecr", "create_repository", repositoryName=n["repository"], tags=tags
                    )
                except ClientError as exc:
                    if _code(exc) != "RepositoryAlreadyExistsException":
                        raise
            await sandbox.push(
                spec.source_image, await ecr_target(self.session, n["repository"], spec.tag)
            )

            # 2. Log group.
            if not await self._log_group_exists(n["log_group"]):
                await self._aws(
                    "logs", "create_log_group", logGroupName=n["log_group"], tags=tag_map
                )
                await self._aws(
                    "logs",
                    "put_retention_policy",
                    logGroupName=n["log_group"],
                    retentionInDays=self.settings.gc_log_retention_days,
                )

            # 3. Security group: the app's port from GC_INGRESS_CIDR.
            group = await self._security_group(n["security_group"], vpc_id)
            if group is None:
                made = await self._aws(
                    "ec2",
                    "create_security_group",
                    GroupName=n["security_group"],
                    Description=f"Groundcrew app {n['app']}",
                    VpcId=vpc_id,
                    TagSpecifications=[{"ResourceType": "security-group", "Tags": tags}],
                )
                group = str(made["GroupId"])
            try:
                await self._aws(
                    "ec2",
                    "authorize_security_group_ingress",
                    GroupId=group,
                    IpPermissions=[
                        {
                            "IpProtocol": "tcp",
                            "FromPort": plan.port,
                            "ToPort": plan.port,
                            "IpRanges": [
                                {
                                    "CidrIp": self.settings.gc_ingress_cidr,
                                    "Description": "Groundcrew app",
                                }
                            ],
                        }
                    ],
                )
            except ClientError as exc:
                if _code(exc) != "InvalidPermission.Duplicate":
                    raise

            # 4. Secrets: written, never read back (ECS injects them with the execution role).
            arns: dict[str, str] = {}
            for name, value in secret_values(spec).items():
                full = secret_name(plan.app, name)
                arn = await self._secret_arn(full)
                if arn is None:
                    made = await self._aws(
                        "secretsmanager",
                        "create_secret",
                        Name=full,
                        Description=f"Groundcrew app {n['app']}: {name}",
                        SecretString=value,
                        Tags=tags,
                    )
                    arn = str(made["ARN"])
                else:
                    await self._aws(
                        "secretsmanager", "put_secret_value", SecretId=full, SecretString=value
                    )
                arns[name] = arn

            # 5. Task definition: a new revision; old ones stay for rollback.
            definition = self._task_definition(plan, account, arns)
            registered = await self._aws(
                "ecs",
                "register_task_definition",
                **definition,
                tags=[{"key": t["Key"], "value": t["Value"]} for t in tags],
            )
            revision = str(registered["taskDefinition"]["taskDefinitionArn"])

            # 6. Service.
            network = {
                "awsvpcConfiguration": {
                    "subnets": subnets,
                    "securityGroups": [group],
                    "assignPublicIp": "ENABLED",
                }
            }
            if await self._service(n["service"]) is None:
                made = await self._aws(
                    "ecs",
                    "create_service",
                    cluster=self.cluster,
                    serviceName=n["service"],
                    taskDefinition=revision,
                    desiredCount=plan.replicas,
                    launchType="FARGATE",
                    networkConfiguration=network,
                    propagateTags="SERVICE",
                    tags=[{"key": t["Key"], "value": t["Value"]} for t in tags],
                )
            else:
                made = await self._aws(
                    "ecs",
                    "update_service",
                    cluster=self.cluster,
                    service=n["service"],
                    taskDefinition=revision,
                    desiredCount=plan.replicas,
                    networkConfiguration=network,
                    forceNewDeployment=True,
                )
            service_arn = str(made["service"]["serviceArn"])
        except ClientError as exc:
            raise self._error(f"{exc.operation_name}", exc) from None
        return Deployment(
            target="aws_ecs",
            connection_id=plan.connection_id,
            run_id=plan.run_id,
            app=plan.app,
            image=plan.image,
            revision=revision,
            previous_revision=plan.previous_revision,
            location=f"{self.cluster}/{n['service']}",
            port=plan.port,
            health_path=plan.health_path,
            external_id=service_arn,
        )

    # --- verify and logs ------------------------------------------------------------------

    async def _stopped_reasons(self, service: str) -> str:
        found = await self._aws(
            "ecs", "list_tasks", cluster=self.cluster, serviceName=service, desiredStatus="STOPPED"
        )
        if not found.get("taskArns"):
            return "no stopped tasks"
        tasks = await self._aws(
            "ecs", "describe_tasks", cluster=self.cluster, tasks=found["taskArns"][:3]
        )
        return "; ".join(redact(str(t.get("stoppedReason", "?"))) for t in tasks.get("tasks", []))

    async def _task_ip(self, service: str, revision: str | None) -> str | None:
        found = await self._aws("ecs", "list_tasks", cluster=self.cluster, serviceName=service)
        if not found.get("taskArns"):
            return None
        tasks = await self._aws(
            "ecs", "describe_tasks", cluster=self.cluster, tasks=found["taskArns"]
        )
        for task in tasks.get("tasks", []):
            if revision and task.get("taskDefinitionArn") != revision:
                continue
            if task.get("lastStatus") != "RUNNING":
                continue
            eni = next(
                (
                    d["value"]
                    for a in task.get("attachments", [])
                    for d in a.get("details", [])
                    if d.get("name") == "networkInterfaceId"
                ),
                None,
            )
            if eni is None:
                continue
            nic = await self._aws("ec2", "describe_network_interfaces", NetworkInterfaceIds=[eni])
            ip = nic["NetworkInterfaces"][0].get("Association", {}).get("PublicIp")
            if ip:
                return str(ip)
        return None

    async def verify(self, deployment: Deployment) -> Health:
        started = time.monotonic()
        deadline = started + self.settings.gc_verify_timeout_seconds
        service = deployment.location.split("/", 1)[1]
        checks: list[VerifyCheck] = []

        rollout: VerifyCheck | None = None
        while rollout is None:
            found = await self._service(service) or {}
            primary: dict[str, Any] = next(
                (d for d in found.get("deployments", []) if d.get("status") == "PRIMARY"), {}
            )
            state = primary.get("rolloutState")
            # Right after CreateService the deployment's counts are still 0 of 0: stable means
            # the service's own desired count of tasks runs the new task definition.
            want = max(int(found.get("desiredCount") or 0), 1)
            running = int(primary.get("runningCount") or 0)
            ours = not deployment.revision or primary.get("taskDefinition") == deployment.revision
            settled = state == "COMPLETED" or (
                state is None and len(found.get("deployments", [])) == 1
            )
            if ours and settled and running >= want:
                rollout = VerifyCheck(
                    name="rollout", ok=True, detail=f"service stable, {running} task running"
                )
            elif state == "FAILED" or time.monotonic() >= deadline:
                reason = primary.get("rolloutStateReason") or (
                    f"not stable after {int(self.settings.gc_verify_timeout_seconds)} s"
                )
                rollout = VerifyCheck(
                    name="rollout",
                    ok=False,
                    detail=f"{redact(str(reason))[:200]} ({await self._stopped_reasons(service)})",
                )
            else:
                await self._sleep(10.0)
        checks.append(rollout)

        live_url = None
        if rollout.ok:
            ip = await self._task_ip(service, deployment.revision)
            if ip is None:
                checks.append(
                    VerifyCheck(name="health", ok=False, detail="the task has no public IP")
                )
            else:
                live_url = f"http://{ip}:{deployment.port}"
                checks += await self._http_checks(live_url, deployment.health_path, deadline)
        decisive = [c for c in checks if c.name != "root" or not deployment.health_path]
        ok = all(c.ok for c in decisive)
        return Health(
            ok=ok,
            checks=checks,
            duration_ms=int((time.monotonic() - started) * 1000),
            live_url=live_url if ok else None,
            logs_tail=None if ok else await self._logs_tail(deployment),
        )

    async def _http_checks(
        self, base: str, health_path: str | None, deadline: float
    ) -> list[VerifyCheck]:
        client = self._http or httpx.AsyncClient(timeout=5.0)
        out: list[VerifyCheck] = []
        try:
            paths: list[tuple[Literal["health", "root"], str]] = []
            if health_path:
                paths.append(("health", health_path))
            paths.append(("root", "/"))
            for name, path in paths:
                last, code, latency = "no answer", None, None
                while True:
                    t0 = time.monotonic()
                    try:
                        response = await client.get(f"{base}{path}")
                        code, latency = response.status_code, int((time.monotonic() - t0) * 1000)
                        last = f"HTTP {code}"
                        if code < 400:
                            break
                    except httpx.HTTPError as exc:
                        last = type(exc).__name__
                    if name == "root" or time.monotonic() >= deadline:
                        break
                    await self._sleep(5.0)
                out.append(
                    VerifyCheck(
                        name=name,
                        ok=code is not None and code < 400,
                        detail=last,
                        url=f"{base}{path}",
                        status_code=code,
                        latency_ms=latency,
                    )
                )
        finally:
            if self._http is None:
                await client.aclose()
        return out

    async def _logs_tail(self, deployment: Deployment) -> str | None:
        lines = await self.logs(deployment, datetime.fromtimestamp(time.time() - 600, UTC))
        if not lines:
            return None
        return "\n".join(f"{line.source}: {line.message}" for line in lines[-LOG_TAIL_LINES:])

    async def logs(self, deployment: Deployment, since: datetime) -> list[LogLine]:
        """CloudWatch /groundcrew/<app>, this app's streams, oldest first (up to 3 pages)."""
        n = self._names(deployment.app)
        out: list[LogLine] = []
        token: str | None = None
        for _ in range(3):
            try:
                page = await self._aws(
                    "logs",
                    "filter_log_events",
                    logGroupName=n["log_group"],
                    logStreamNamePrefix=n["app"],
                    startTime=int(since.timestamp() * 1000),
                    limit=500,
                    **({"nextToken": token} if token else {}),
                )
            except ClientError as exc:
                if _code(exc) == "ResourceNotFoundException":
                    return out
                raise self._error("logs:FilterLogEvents", exc) from None
            for event in page.get("events", []):
                out.append(
                    LogLine(
                        at=datetime.fromtimestamp(event["timestamp"] / 1000, UTC),
                        source=str(event.get("logStreamName", "")),
                        message=redact(str(event.get("message", "")).rstrip()),
                    )
                )
            token = page.get("nextToken")
            if not token:
                break
        return out

    # --- rollback -------------------------------------------------------------------------

    async def rollback_plan(self, deployment: Deployment) -> TargetPlan:
        """Back to the previous task definition, or delete the service of a first deploy."""
        n = self._names(deployment.app)
        to = deployment.previous_revision
        step = (
            ApplyStep(
                kind="ecs:Service",
                name=n["service"],
                action="update",
                detail=f"task definition {to.rsplit('/', 1)[-1]}",
            )
            if to
            else ApplyStep(
                kind="ecs:Service",
                name=n["service"],
                action="delete",
                detail="first deploy: scales to 0 and deletes the service; the image, log "
                "group, security group, and secrets stay until make cleanup-deploys",
            )
        )
        return TargetPlan(
            action="rollback",
            target="aws_ecs",
            connection_id=deployment.connection_id,
            run_id=deployment.run_id,
            app=n["app"],
            image=deployment.image,
            source_image_id=None,
            image_tested=True,
            port=deployment.port,
            health_path=deployment.health_path,
            replicas=1,
            cpu=CPU,
            memory=MEMORY,
            env=[],
            allow_missing_env=False,
            steps=[step],
            rendered=json.dumps(
                {
                    "cluster": self.cluster,
                    "service": n["service"],
                    "from_revision": deployment.revision,
                    "to_revision": to,
                },
                indent=2,
            ),
            rendered_format="json",
            previous_revision=deployment.revision,
            rollback_to_revision=to,
        )

    async def rollback(self, plan: TargetPlan, token: ApprovedAction) -> Deployment:
        await consume_approved_action(
            self.session_factory,
            token,
            run_id=plan.run_id,
            action=TARGET_ROLLBACK,
            plan_hash=plan.plan_hash(),
        )
        n = self._names(plan.app)
        to = plan.rollback_to_revision
        try:
            if to is None:
                await self._aws(
                    "ecs",
                    "update_service",
                    cluster=self.cluster,
                    service=n["service"],
                    desiredCount=0,
                )
                await self._aws(
                    "ecs", "delete_service", cluster=self.cluster, service=n["service"], force=True
                )
                image = plan.image
            else:
                await self._aws(
                    "ecs",
                    "update_service",
                    cluster=self.cluster,
                    service=n["service"],
                    taskDefinition=to,
                    forceNewDeployment=True,
                )
                definition = await self._aws("ecs", "describe_task_definition", taskDefinition=to)
                image = str(definition["taskDefinition"]["containerDefinitions"][0]["image"])
        except ClientError as exc:
            raise self._error(f"{exc.operation_name}", exc) from None
        return Deployment(
            target="aws_ecs",
            connection_id=plan.connection_id,
            run_id=plan.run_id,
            app=plan.app,
            image=image,
            revision=to,
            previous_revision=plan.previous_revision,
            location=f"{self.cluster}/{n['service']}",
            port=plan.port,
            health_path=plan.health_path,
            status="deleted" if to is None else "rolled_back",
        )
