"""In-memory emergency actions with the real approval checks. Medic's tests use it, and so
does Medic until the Operate-tier implementation lands (docs/phase5-interfaces.md).

State is a dict a test sets up: task definition per service, version stages per secret. Plans
read it; `execute` consumes the token exactly like the real one (single use, same run, action,
and plan hash) and then changes the dict.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.actions.base import (
    ECS_ROLLBACK_SERVICE,
    FORCE_REDEPLOY,
    SECRET_ROLLBACK_VERSION,
    ActionPlan,
    ActionResult,
    EmergencyActionError,
)
from app.db.session import SessionFactory
from app.policy.actions import ApprovedAction, consume_approved_action


@dataclass
class FakeService:
    # Task definition ARNs, oldest first; the last one is what the service runs.
    revisions: list[str]
    deployment_id: str = "ecs-svc/1"
    deployments: int = 1


@dataclass
class FakeSecret:
    # version id -> stages
    versions: dict[str, list[str]] = field(default_factory=dict)

    def holding(self, stage: str) -> str | None:
        return next((v for v, stages in self.versions.items() if stage in stages), None)


class FakeEmergencyActions:
    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        services: dict[str, FakeService] | None = None,
        secrets: dict[str, FakeSecret] | None = None,
    ) -> None:
        self.session_factory = session_factory
        # "cluster/service" -> state; secret ARN -> state
        self.services = services or {}
        self.secrets = secrets or {}
        self.executed: list[ActionPlan] = []

    def _service(self, cluster: str, service: str) -> FakeService:
        try:
            return self.services[f"{cluster}/{service}"]
        except KeyError:
            raise EmergencyActionError(f"Service {service} not found in {cluster}.") from None

    def _secret(self, secret_arn: str) -> FakeSecret:
        try:
            return self.secrets[secret_arn]
        except KeyError:
            raise EmergencyActionError(f"Secret {secret_arn} not found.") from None

    async def plan_ecs_rollback_service(
        self, connection_id: uuid.UUID, cluster: str, service: str
    ) -> ActionPlan:
        state = self._service(cluster, service)
        if len(state.revisions) < 2:
            raise EmergencyActionError(f"{service} has no previous task definition revision.")
        current, previous = state.revisions[-1], state.revisions[-2]
        return ActionPlan(
            action=ECS_ROLLBACK_SERVICE,
            connection_id=connection_id,
            target=f"{cluster}/{service}",
            params={"cluster": cluster, "service": service},
            current=current,
            desired=previous,
            summary=f"Point {service} back at {_short(previous)} (now {_short(current)})",
            planned_at=datetime.now(UTC),
        )

    async def plan_secret_rollback_version(
        self, connection_id: uuid.UUID, secret_arn: str
    ) -> ActionPlan:
        state = self._secret(secret_arn)
        current, previous = state.holding("AWSCURRENT"), state.holding("AWSPREVIOUS")
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
        state = self._service(cluster, service)
        return ActionPlan(
            action=FORCE_REDEPLOY,
            connection_id=connection_id,
            target=f"{cluster}/{service}",
            params={"cluster": cluster, "service": service},
            current=state.deployment_id,
            desired=None,
            summary=f"Start new {service} tasks on {_short(state.revisions[-1])}",
            planned_at=datetime.now(UTC),
        )

    def _now_state(self, plan: ActionPlan) -> str | None:
        if plan.action == SECRET_ROLLBACK_VERSION:
            return self._secret(plan.params["secret_arn"]).holding("AWSCURRENT")
        state = self._service(plan.params["cluster"], plan.params["service"])
        return state.revisions[-1] if plan.action == ECS_ROLLBACK_SERVICE else state.deployment_id

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
        )
        if self._now_state(plan) != plan.current:
            return ActionResult(
                action=plan.action,
                ok=False,
                summary="Not run: the state changed since the plan was approved.",
                error=f"expected {plan.current}, found {self._now_state(plan)}",
                started_at=started,
                finished_at=datetime.now(UTC),
            )
        details: dict[str, str] = {}
        if plan.action == SECRET_ROLLBACK_VERSION and plan.current and plan.desired:
            secret = self._secret(plan.params["secret_arn"])
            secret.versions[plan.current] = ["AWSPREVIOUS"]
            secret.versions[plan.desired] = ["AWSCURRENT"]
            details = {"current_version": plan.desired, "previous_version": plan.current}
        else:
            service = self._service(plan.params["cluster"], plan.params["service"])
            if plan.action == ECS_ROLLBACK_SERVICE and plan.desired:
                service.revisions.append(plan.desired)
                details["task_definition"] = plan.desired
            service.deployments += 1
            service.deployment_id = f"ecs-svc/{service.deployments}"
            details["deployment_id"] = service.deployment_id
        self.executed.append(plan)
        return ActionResult(
            action=plan.action,
            ok=True,
            summary=plan.summary,
            details=details,
            started_at=started,
            finished_at=datetime.now(UTC),
        )


def _short(arn: str) -> str:
    """checkout-worker:41 from a task definition ARN, the name from a secret ARN."""
    if ":secret:" in arn:
        return arn.split(":secret:", 1)[1]
    return arn.rsplit("/", 1)[-1]
