"""Emergency actions on AWS through the groundcrew-operate role (EnableOperate=true).

Every call goes through `CredentialsProvider.aws_operate_session()`, which refuses unless the
connection has the operate capability and refuses any operation outside
AWS_OPERATE_OPERATIONS. The IAM policy limits writes to the stack's OperateServiceArns and
OperateSecretArns. `execute` consumes the approved action before its first write and re-reads
the state first: if it no longer matches the plan, nothing is written (ok=False).

- ecs_rollback_service: UpdateService to the newest ACTIVE revision of the family older than
  the one running now (task definitions are never deregistered by Groundcrew). ECS checks
  iam:PassRole on that revision's execution and task roles, so the plan first reads the
  operate role's PassRole scope (OperatePassRoleArns, via the read-only role's
  iam:GetRolePolicy) and stops with "rollback needs PassRole on ..." when a role is outside it,
  instead of an approval that AWS would refuse.
- secret_rollback_version: UpdateSecretVersionStage moves AWSCURRENT to the version holding
  AWSPREVIOUS, by version id. GetSecretValue is never called (and denied by the role).
- force_redeploy: UpdateService(forceNewDeployment=True), so new tasks read secrets again.
- lambda_rollback_alias: UpdateAlias points the alias callers use (the one on the newest
  published version; live / prod / production first) back at the previous published version,
  and clears weighted routing. Functions called as $LATEST have no alias to move: no plan.
- ec2_reboot_instance: RebootInstances on a running instance whose status check is impaired.
  Not run if the instance recovered or stopped since the plan.
"""

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Any
from urllib.parse import unquote

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select

from app.actions.base import (
    EC2_REBOOT_INSTANCE,
    ECS_ROLLBACK_SERVICE,
    FORCE_REDEPLOY,
    LAMBDA_ROLLBACK_ALIAS,
    SECRET_ROLLBACK_VERSION,
    ActionPlan,
    ActionResult,
    EmergencyActionError,
)
from app.config import Settings, get_settings
from app.connectors.aws import (
    DENIED_CODES,
    arn_region,
    connection_regions,
    error_code,
    error_text,
)
from app.connectors.credentials import ConnectionCredentialsProvider, CredentialsError
from app.db.models import Connection
from app.db.session import SessionFactory
from app.policy.actions import ApprovedAction, consume_approved_action
from app.telemetry import aws_compute

Client = Any

logger = logging.getLogger("groundcrew.actions")

ECS_TASKS = "ecs-tasks.amazonaws.com"
# The template's explicit Deny (NeverPassGroundcrewRoles), whatever OperatePassRoleArns says.
NEVER_PASSED = "arn:*:iam::*:role/groundcrew-*"


def _short(arn: str | None) -> str:
    if not arn:
        return "-"
    if ":secret:" in arn:
        return arn.split(":secret:", 1)[1]
    return arn.rsplit("/", 1)[-1]


def _listed(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def pass_role_patterns(document: Any) -> list[str]:
    """Role ARN patterns an inline policy lets the holder pass to ECS tasks: Allow statements
    on iam:PassRole whose iam:PassedToService condition (if any) includes ecs-tasks."""
    if isinstance(document, str):
        document = json.loads(unquote(document))
    out: list[str] = []
    for st in _listed((document or {}).get("Statement")):
        actions = [str(a).lower() for a in _listed(st.get("Action"))]
        if st.get("Effect") != "Allow" or not {"iam:passrole", "iam:*", "*"} & set(actions):
            continue
        services: list[str] = []
        for op, keys in (st.get("Condition") or {}).items():
            for key, value in keys.items():
                if key.lower() == "iam:passedtoservice" and op.startswith("String"):
                    services += [str(v) for v in _listed(value)]
        if services and ECS_TASKS not in services:
            continue
        out += [str(r) for r in _listed(st.get("Resource"))]
    return out


def roles_outside(roles: list[str], patterns: list[str]) -> list[str]:
    """The roles no pattern allows passing (IAM wildcards: * any run, ? one character)."""
    return [
        role
        for role in roles
        if fnmatchcase(role, NEVER_PASSED) or not any(fnmatchcase(role, p) for p in patterns)
    ]


# The alias names callers most likely use, first.
PREFERRED_ALIASES = ("live", "prod", "production", "current", "stable")


def pick_alias(aliases: list[dict[str, Any]], versions: list[str]) -> dict[str, Any] | None:
    """The alias to roll back: one on the newest published version, a conventional name first;
    else any alias on a published version. None when every caller uses $LATEST."""
    on_version = [a for a in aliases if str(a.get("FunctionVersion")) in versions]
    if not on_version:
        return None
    newest = max(int(str(a["FunctionVersion"])) for a in on_version)
    top = [a for a in on_version if int(str(a["FunctionVersion"])) == newest]
    for name in PREFERRED_ALIASES:
        found = next((a for a in top if str(a.get("Name")).lower() == name), None)
        if found is not None:
            return found
    return top[0]


# The stack parameter that lists what the operate role may act on, per AWS service.
OPERATE_PARAMETERS = {
    "ecs": "OperateServiceArns",
    "secretsmanager": "OperateSecretArns",
    "lambda": "OperateFunctionArns",
    "ec2": "OperateInstanceArns",
    "iam": "OperatePassRoleArns",
}
# How many earlier task definition revisions a rollback looks at for one whose images exist.
ROLLBACK_CANDIDATES = 5
_ECR_IMAGE = re.compile(
    r"^(?P<account>\d{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com/"
    r"(?P<repo>[^:@]+)(?::(?P<tag>[^@]+))?(?:@(?P<digest>sha256:[0-9a-f]{64}))?$"
)


def denied_text(client: Client, operation: str, resource: str | None) -> str:
    """What to change when the operate role is refused, instead of AWS's raw AccessDenied."""
    service = str(client.meta.service_model.service_name)
    api = client.meta.method_to_api_mapping.get(operation, operation)
    what = resource or "this resource"
    parameter = OPERATE_PARAMETERS.get(service)
    where = (
        f"Add it to {parameter} on the groundcrew-read-only stack (CloudFormation, Update, "
        "keep the current template), then run Medic again."
        if parameter
        else "Check the groundcrew-operate role on the groundcrew-read-only stack."
    )
    return f"Groundcrew's emergency-fix role may not use {service}:{api} on {what}. {where}"


def ecr_image(image: str) -> dict[str, str] | None:
    """registry account, region, repository, and tag or digest of an ECR image reference."""
    found = _ECR_IMAGE.match(image)
    if not found:
        return None
    parts = {k: v for k, v in found.groupdict().items() if v}
    if "digest" not in parts:
        parts.setdefault("tag", "latest")
    return parts


def _revision(arn: str) -> tuple[str, int]:
    """family, revision from a task definition ARN (...:task-definition/family:42)."""
    family, _, rev = arn.rsplit("/", 1)[-1].rpartition(":")
    return family, int(rev)


class AwsEmergencyActions:
    def __init__(
        self,
        session_factory: SessionFactory,
        settings: Settings | None = None,
        *,
        client_for: Callable[[Connection, str, str], Client] | None = None,
        read_client_for: Callable[[Connection, str, str], Client] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self._client_for = client_for or self._operate_client
        self._read_client_for = read_client_for or self._read_client

    def _operate_client(self, conn: Connection, service: str, region: str) -> Client:
        session = ConnectionCredentialsProvider([conn]).aws_operate_session(conn.id)
        return session.client(service, region_name=region)

    def _read_client(self, conn: Connection, service: str, region: str) -> Client:
        session = ConnectionCredentialsProvider([conn]).aws_session(conn.id)
        return session.client(service, region_name=region)

    def _region(self, conn: Connection, arn: str | None) -> str:
        """The region of the resource acted on (from its ARN), which must be one of the
        connection's regions; the connection's first region when there is no ARN."""
        allowed = connection_regions(conn.config, self.settings)
        region = arn_region(arn) or allowed[0]
        if region not in allowed:
            raise EmergencyActionError(
                f"{region} is not one of this connection's regions ({', '.join(allowed)})."
            )
        return region

    async def _connection(self, connection_id: uuid.UUID) -> Connection:
        async with self.session_factory() as session:
            conn = await session.scalar(select(Connection).where(Connection.id == connection_id))
        if conn is None or conn.kind != "aws":
            raise EmergencyActionError("The AWS connection no longer exists.")
        return conn

    async def _client(
        self, connection_id: uuid.UUID, service: str, arn: str | None = None
    ) -> Client:
        conn = await self._connection(connection_id)
        region = self._region(conn, arn)
        try:
            return await asyncio.to_thread(self._client_for, conn, service, region)
        except CredentialsError as exc:
            raise EmergencyActionError(str(exc)) from None

    async def _pass_role_patterns(self, connection_id: uuid.UUID) -> list[str] | None:
        """What the operate role may pass to ECS tasks (its stack's OperatePassRoleArns), read
        from its inline policy with the read-only role; None when that cannot be read (the plan
        then goes ahead and AWS has the last word at execute time)."""
        conn = await self._connection(connection_id)
        role = str((conn.config or {}).get("operate_role_arn") or "").rsplit("/", 1)[-1]
        if not role:
            return None
        try:
            region = connection_regions(conn.config, self.settings)[0]
            iam = await asyncio.to_thread(self._read_client_for, conn, "iam", region)
            found = await self._call(iam, "get_role_policy", RoleName=role, PolicyName=role)
            return pass_role_patterns(found.get("PolicyDocument"))
        except (CredentialsError, EmergencyActionError, ValueError):
            logger.warning("could not read the operate role's PassRole scope", exc_info=True)
            return None

    @staticmethod
    async def _call(
        client: Client, operation: str, *, resource: str | None = None, **params: Any
    ) -> Any:
        try:
            return await asyncio.to_thread(getattr(client, operation), **params)
        except ClientError as exc:
            if error_code(exc) in DENIED_CODES:
                raise EmergencyActionError(denied_text(client, operation, resource)) from None
            raise EmergencyActionError(error_text(exc)) from None
        except BotoCoreError as exc:
            raise EmergencyActionError(error_text(exc)) from None

    async def _service(self, ecs: Client, cluster: str, service: str) -> dict[str, Any]:
        found = await self._call(
            ecs,
            "describe_services",
            resource=f"service {_short(cluster)}/{service}",
            cluster=cluster,
            services=[service],
        )
        services = found.get("services") or []
        if not services:
            raise EmergencyActionError(f"Service {service} was not found in {_short(cluster)}.")
        result: dict[str, Any] = services[0]
        return result

    @staticmethod
    def _primary(service: dict[str, Any]) -> str | None:
        primary = next(
            (d for d in service.get("deployments", []) if d.get("status") == "PRIMARY"), None
        )
        return str(primary["id"]) if primary and primary.get("id") else None

    # ------------------------------------------------------------------- plans (read)

    async def plan_ecs_rollback_service(
        self, connection_id: uuid.UUID, cluster: str, service: str
    ) -> ActionPlan:
        ecs = await self._client(connection_id, "ecs", cluster)
        current = str((await self._service(ecs, cluster, service)).get("taskDefinition") or "")
        if not current:
            raise EmergencyActionError(f"{service} has no task definition.")
        family, revision = _revision(current)
        listed = await self._call(
            ecs,
            "list_task_definitions",
            familyPrefix=family,
            status="ACTIVE",
            sort="DESC",
            maxResults=100,
        )
        older = [
            arn
            for arn in listed.get("taskDefinitionArns", [])
            if _revision(arn)[0] == family and _revision(arn)[1] < revision
        ]
        if not older:
            raise EmergencyActionError(f"{service} has no earlier task definition revision.")
        previous, definition = await self._startable_revision(
            connection_id, ecs, cluster, service, sorted(older, key=lambda a: -_revision(a)[1])
        )
        roles = [
            str(definition[k]) for k in ("executionRoleArn", "taskRoleArn") if definition.get(k)
        ]
        if roles:
            patterns = await self._pass_role_patterns(connection_id)
            missing = roles_outside(roles, patterns) if patterns is not None else []
            if missing:
                raise EmergencyActionError(
                    f"rollback needs PassRole on {', '.join(dict.fromkeys(missing))}; update the "
                    "connection: add the role to OperatePassRoleArns on the "
                    "groundcrew-read-only stack (EnableOperate=true), then plan again."
                )
        return ActionPlan(
            action=ECS_ROLLBACK_SERVICE,
            connection_id=connection_id,
            target=f"{_short(cluster)}/{service}",
            params={"cluster": cluster, "service": service},
            current=current,
            desired=previous,
            summary=f"Point {service} back at {_short(previous)} (now {_short(current)})",
            planned_at=datetime.now(UTC),
        )

    async def _missing_secrets(
        self, connection_id: uuid.UUID, definition: dict[str, Any]
    ) -> list[str]:
        """The Secrets Manager secrets a task definition reads that no longer exist (a rebuilt
        environment has new ones): ECS would stop its tasks with ResourceInitializationError.
        One that cannot be checked counts as present."""
        arns: list[str] = []
        for container in definition.get("containerDefinitions") or []:
            for secret in container.get("secrets") or []:
                ref = str(secret.get("valueFrom") or "")
                if ":secretsmanager:" in ref and ":secret:" in ref:
                    arn = ":".join(ref.split(":")[:7])  # without a JSON key or version suffix
                    if arn not in arns:
                        arns.append(arn)
        missing: list[str] = []
        reader: Client | None = None
        for arn in arns:
            if reader is None:
                try:
                    reader = await self._read(connection_id, "secretsmanager", arn)
                except EmergencyActionError:
                    return []
            try:
                await asyncio.to_thread(reader.describe_secret, SecretId=arn)
            except ClientError as exc:
                if error_code(exc) == "ResourceNotFoundException":
                    missing.append(_short(arn))
            except BotoCoreError:
                pass
        return missing

    async def _missing_images(
        self, connection_id: uuid.UUID, cluster: str, definition: dict[str, Any]
    ) -> list[str]:
        """The ECR images of a task definition that are no longer in their repository. An image
        that cannot be checked (not ECR, no permission) counts as present: AWS has the last word
        when the rollback runs."""
        missing: list[str] = []
        reader: Client | None = None
        for container in definition.get("containerDefinitions") or []:
            image = str(container.get("image") or "")
            ref = ecr_image(image)
            if ref is None:
                continue
            if reader is None:
                try:
                    reader = await self._read(connection_id, "ecr", cluster)
                except EmergencyActionError:
                    return []
            image_id = (
                {"imageDigest": ref["digest"]} if "digest" in ref else {"imageTag": ref["tag"]}
            )
            try:
                await asyncio.to_thread(
                    reader.describe_images,
                    registryId=ref["account"],
                    repositoryName=ref["repo"],
                    imageIds=[image_id],
                )
            except ClientError as exc:
                if error_code(exc) in ("ImageNotFoundException", "RepositoryNotFoundException"):
                    missing.append(image.rsplit("/", 1)[-1])
            except BotoCoreError:
                pass
        return missing

    async def _startable_revision(
        self,
        connection_id: uuid.UUID,
        ecs: Client,
        cluster: str,
        service: str,
        older: list[str],
    ) -> tuple[str, dict[str, Any]]:
        """The newest earlier revision whose images and secrets still exist, so the rollback can
        start. Raises with what is missing when none of the last few can."""
        gone: list[str] = []
        for arn in older[:ROLLBACK_CANDIDATES]:
            described = await self._call(
                ecs, "describe_task_definition", resource=_short(arn), taskDefinition=arn
            )
            definition: dict[str, Any] = described.get("taskDefinition") or {}
            missing = await self._missing_images(connection_id, cluster, definition)
            missing += [
                f"secret {name}" for name in await self._missing_secrets(connection_id, definition)
            ]
            if not missing:
                return arn, definition
            gone += [m for m in missing if m not in gone]
        checked = ", ".join(_short(a) for a in older[:ROLLBACK_CANDIDATES])
        raise EmergencyActionError(
            f"No earlier revision of {service} can start: what they use "
            f"({', '.join(gone)}) no longer exists (checked {checked}), as after an environment "
            "is rebuilt. A rollback cannot help; push the image again, or deploy a fixed revision."
        )

    async def plan_secret_rollback_version(
        self, connection_id: uuid.UUID, secret_arn: str
    ) -> ActionPlan:
        sm = await self._client(connection_id, "secretsmanager", secret_arn)
        secret = await self._call(sm, "describe_secret", SecretId=secret_arn)
        stages: dict[str, list[str]] = secret.get("VersionIdsToStages") or {}
        current = next((v for v, s in stages.items() if "AWSCURRENT" in s), None)
        previous = next((v for v, s in stages.items() if "AWSPREVIOUS" in s), None)
        if current is None or previous is None:
            raise EmergencyActionError("The secret has no AWSPREVIOUS version to go back to.")
        return ActionPlan(
            action=SECRET_ROLLBACK_VERSION,
            connection_id=connection_id,
            target=secret_arn,
            params={"secret_arn": secret_arn},
            current=current,
            desired=previous,
            summary=f"Move AWSCURRENT of {_short(secret_arn)} back to version {previous}",
            planned_at=datetime.now(UTC),
        )

    async def plan_force_redeploy(
        self, connection_id: uuid.UUID, cluster: str, service: str
    ) -> ActionPlan:
        ecs = await self._client(connection_id, "ecs", cluster)
        found = await self._service(ecs, cluster, service)
        return ActionPlan(
            action=FORCE_REDEPLOY,
            connection_id=connection_id,
            target=f"{_short(cluster)}/{service}",
            params={"cluster": cluster, "service": service},
            current=self._primary(found),
            desired=None,
            summary=f"Start new {service} tasks on {_short(found.get('taskDefinition'))}",
            planned_at=datetime.now(UTC),
        )

    async def _read(self, connection_id: uuid.UUID, service: str, arn: str) -> Client:
        conn = await self._connection(connection_id)
        region = self._region(conn, arn)
        try:
            return await asyncio.to_thread(self._read_client_for, conn, service, region)
        except CredentialsError as exc:
            raise EmergencyActionError(str(exc)) from None

    async def plan_lambda_rollback_alias(
        self, connection_id: uuid.UUID, function_arn: str
    ) -> ActionPlan:
        name = aws_compute.function_name(function_arn)
        reader = await self._read(connection_id, "lambda", function_arn)
        aliases = (await self._call(reader, "list_aliases", FunctionName=name)).get("Aliases", [])
        lam = await self._client(connection_id, "lambda", function_arn)
        versions = aws_compute.published(
            (await self._call(lam, "list_versions_by_function", FunctionName=name)).get(
                "Versions", []
            )
        )
        alias = pick_alias(aliases, [str(v["Version"]) for v in versions])
        if alias is None:
            raise EmergencyActionError(
                f"{name} has no alias on a published version (it runs as $LATEST), so there is "
                "nothing to roll back in place. Fix it through the pull request."
            )
        current = str(alias["FunctionVersion"])
        older = [v for v in versions if int(v["Version"]) < int(current)]
        if not older:
            raise EmergencyActionError(f"{name} has no version before {current}.")
        previous = str(older[-1]["Version"])
        return ActionPlan(
            action=LAMBDA_ROLLBACK_ALIAS,
            connection_id=connection_id,
            target=f"{name}:{alias['Name']}",
            params={"function_arn": function_arn, "alias": str(alias["Name"])},
            current=aws_compute.version_ref(function_arn, current),
            desired=aws_compute.version_ref(function_arn, previous),
            summary=f"Point {name}:{alias['Name']} back at version {previous} (now {current})",
            planned_at=datetime.now(UTC),
        )

    async def _instance_state(self, ec2: Client, instance: str) -> str:
        found = await self._call(
            ec2, "describe_instance_status", InstanceIds=[instance], IncludeAllInstances=True
        )
        status = (found.get("InstanceStatuses") or [{}])[0]
        state = (status.get("InstanceState") or {}).get("Name") or "unknown"
        checks = [
            f"{label} {(status.get(key) or {}).get('Status') or '?'}"
            for key, label in (("SystemStatus", "system"), ("InstanceStatus", "instance"))
        ]
        return f"{instance} {state}, {', '.join(checks)}"

    async def plan_ec2_reboot_instance(
        self, connection_id: uuid.UUID, instance_arn: str
    ) -> ActionPlan:
        instance = instance_arn.rsplit("/", 1)[-1]
        ec2 = await self._client(connection_id, "ec2", instance_arn)
        state = await self._instance_state(ec2, instance)
        if " running," not in state or "impaired" not in state:
            raise EmergencyActionError(
                f"No reboot: {state}. Only a running instance with a failed status check is "
                "rebooted."
            )
        return ActionPlan(
            action=EC2_REBOOT_INSTANCE,
            connection_id=connection_id,
            target=instance,
            params={"instance_arn": instance_arn},
            current=state,
            desired=None,
            summary=f"Reboot {instance} ({state.split(', ', 1)[-1]})",
            planned_at=datetime.now(UTC),
        )

    # ------------------------------------------------------------------ execute (write)

    async def execute(
        self, plan: ActionPlan, token: ApprovedAction, *, run_id: uuid.UUID
    ) -> ActionResult:
        started = datetime.now(UTC)
        await consume_approved_action(
            self.session_factory,
            token,
            run_id=run_id,
            action=plan.action,
            plan_hash=plan.plan_hash(),
            settings=self.settings,
        )

        def result(ok: bool, summary: str, **kw: Any) -> ActionResult:
            return ActionResult(
                action=plan.action,
                ok=ok,
                summary=summary,
                started_at=started,
                finished_at=datetime.now(UTC),
                **kw,
            )

        try:
            if plan.action == LAMBDA_ROLLBACK_ALIAS:
                arn, alias = plan.params["function_arn"], plan.params["alias"]
                name = aws_compute.function_name(arn)
                lam = await self._client(plan.connection_id, "lambda", arn)
                found = await self._call(lam, "get_alias", FunctionName=name, Name=alias)
                points_at = aws_compute.version_ref(arn, str(found.get("FunctionVersion")))
                if points_at != plan.current or plan.desired is None:
                    return result(
                        False,
                        "Not run: the alias moved since the plan was approved.",
                        error=f"{alias} is at {points_at}, the plan expected {plan.current}",
                    )
                version = plan.desired.rsplit(":", 1)[-1]
                await self._call(
                    lam,
                    "update_alias",
                    FunctionName=name,
                    Name=alias,
                    FunctionVersion=version,
                    RoutingConfig={"AdditionalVersionWeights": {}},
                )
                return result(True, plan.summary, details={"alias": alias, "version": version})
            if plan.action == EC2_REBOOT_INSTANCE:
                arn = plan.params["instance_arn"]
                instance = arn.rsplit("/", 1)[-1]
                ec2 = await self._client(plan.connection_id, "ec2", arn)
                state = await self._instance_state(ec2, instance)
                if state != plan.current:
                    return result(
                        False,
                        "Not run: the instance changed since the plan was approved.",
                        error=f"now {state}, the plan expected {plan.current}",
                    )
                await self._call(ec2, "reboot_instances", InstanceIds=[instance])
                return result(True, plan.summary, details={"instance": instance})
            if plan.action == SECRET_ROLLBACK_VERSION:
                sm = await self._client(
                    plan.connection_id, "secretsmanager", plan.params["secret_arn"]
                )
                secret = await self._call(sm, "describe_secret", SecretId=plan.params["secret_arn"])
                stages: dict[str, list[str]] = secret.get("VersionIdsToStages") or {}
                now = next((v for v, s in stages.items() if "AWSCURRENT" in s), None)
                if now != plan.current or plan.desired is None:
                    return result(
                        False,
                        "Not run: the secret changed since the plan was approved.",
                        error=f"AWSCURRENT is {now}, the plan expected {plan.current}",
                    )
                await self._call(
                    sm,
                    "update_secret_version_stage",
                    SecretId=plan.params["secret_arn"],
                    VersionStage="AWSCURRENT",
                    MoveToVersionId=plan.desired,
                    RemoveFromVersionId=plan.current,
                )
                return result(
                    True,
                    plan.summary,
                    details={
                        "current_version": plan.desired,
                        "previous_version": str(plan.current),
                    },
                )
            ecs = await self._client(plan.connection_id, "ecs", plan.params["cluster"])
            cluster, service = plan.params["cluster"], plan.params["service"]
            found = await self._service(ecs, cluster, service)
            if plan.action == ECS_ROLLBACK_SERVICE:
                now = str(found.get("taskDefinition") or "")
                if now != plan.current or plan.desired is None:
                    return result(
                        False,
                        "Not run: the service changed since the plan was approved.",
                        error=f"it runs {_short(now)}, the plan expected {_short(plan.current)}",
                    )
                updated = await self._call(
                    ecs,
                    "update_service",
                    cluster=cluster,
                    service=service,
                    taskDefinition=plan.desired,
                )
            else:
                now_primary = self._primary(found)
                if now_primary != plan.current:
                    return result(
                        False,
                        "Not run: a new deployment started since the plan was approved.",
                        error=f"primary is {now_primary}, the plan expected {plan.current}",
                    )
                updated = await self._call(
                    ecs,
                    "update_service",
                    cluster=cluster,
                    service=service,
                    forceNewDeployment=True,
                )
            deployment = self._primary(updated.get("service") or {}) or ""
            details = {"deployment_id": deployment}
            if plan.action == ECS_ROLLBACK_SERVICE and plan.desired:
                details["task_definition"] = plan.desired
            return result(True, plan.summary, details=details)
        except EmergencyActionError as exc:
            return result(False, "AWS refused the action.", error=str(exc))
