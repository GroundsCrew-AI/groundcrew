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
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Any
from urllib.parse import unquote

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select

from app.actions.base import (
    ECS_ROLLBACK_SERVICE,
    FORCE_REDEPLOY,
    SECRET_ROLLBACK_VERSION,
    ActionPlan,
    ActionResult,
    EmergencyActionError,
)
from app.config import Settings, get_settings
from app.connectors.aws import error_text
from app.connectors.credentials import ConnectionCredentialsProvider, CredentialsError
from app.db.models import Connection
from app.db.session import SessionFactory
from app.policy.actions import ApprovedAction, consume_approved_action

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
        client_for: Callable[[Connection, str], Client] | None = None,
        read_client_for: Callable[[Connection, str], Client] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self._client_for = client_for or self._operate_client
        self._read_client_for = read_client_for or self._read_client

    def _operate_client(self, conn: Connection, service: str) -> Client:
        session = ConnectionCredentialsProvider([conn]).aws_operate_session(conn.id)
        return session.client(service, region_name=self.settings.discovery_regions[0])

    def _read_client(self, conn: Connection, service: str) -> Client:
        session = ConnectionCredentialsProvider([conn]).aws_session(conn.id)
        return session.client(service, region_name=self.settings.discovery_regions[0])

    async def _connection(self, connection_id: uuid.UUID) -> Connection:
        async with self.session_factory() as session:
            conn = await session.scalar(select(Connection).where(Connection.id == connection_id))
        if conn is None or conn.kind != "aws":
            raise EmergencyActionError("The AWS connection no longer exists.")
        return conn

    async def _client(self, connection_id: uuid.UUID, service: str) -> Client:
        conn = await self._connection(connection_id)
        try:
            return await asyncio.to_thread(self._client_for, conn, service)
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
            iam = await asyncio.to_thread(self._read_client_for, conn, "iam")
            found = await self._call(iam, "get_role_policy", RoleName=role, PolicyName=role)
            return pass_role_patterns(found.get("PolicyDocument"))
        except (CredentialsError, EmergencyActionError, ValueError):
            logger.warning("could not read the operate role's PassRole scope", exc_info=True)
            return None

    @staticmethod
    async def _call(client: Client, operation: str, **params: Any) -> Any:
        try:
            return await asyncio.to_thread(getattr(client, operation), **params)
        except (ClientError, BotoCoreError) as exc:
            raise EmergencyActionError(error_text(exc)) from None

    async def _service(self, ecs: Client, cluster: str, service: str) -> dict[str, Any]:
        found = await self._call(ecs, "describe_services", cluster=cluster, services=[service])
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
        ecs = await self._client(connection_id, "ecs")
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
        previous = max(older, key=lambda arn: _revision(arn)[1])
        described = await self._call(ecs, "describe_task_definition", taskDefinition=previous)
        definition = described.get("taskDefinition") or {}
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

    async def plan_secret_rollback_version(
        self, connection_id: uuid.UUID, secret_arn: str
    ) -> ActionPlan:
        sm = await self._client(connection_id, "secretsmanager")
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
        ecs = await self._client(connection_id, "ecs")
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
            if plan.action == SECRET_ROLLBACK_VERSION:
                sm = await self._client(plan.connection_id, "secretsmanager")
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
            ecs = await self._client(plan.connection_id, "ecs")
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
