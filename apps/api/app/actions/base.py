"""Emergency actions on a customer's existing resources (docs/phase5-interfaces.md).

Rizwan owns this package and implements `EmergencyActions` on an Operate session
(`CredentialsProvider.aws_operate_session`, connection capability `operate`). Medic (Phase 5)
only calls it. Until the real implementation lands, Medic and its tests use
`FakeEmergencyActions` (app/actions/fake.py).

Every action is two calls, like `Target.rollback_plan()` / `rollback()`:

1. `plan_*` reads the current state (no writes) and returns an `ActionPlan`: exactly what will
   change, from what to what. The plan is what a person approves (`plan_hash()` binds it).
2. `execute(plan, token)` runs that plan once. It refuses without an `ApprovedAction` for the
   same run, action, and plan hash (`app.policy.actions.consume_approved_action`, called before
   the first write), and refuses if the state moved since the plan (`current` no longer
   matches), so an approval never applies to a situation the approver did not see.

Secret values are never read: a secret rollback moves the AWSCURRENT stage back to the version
that holds AWSPREVIOUS (UpdateSecretVersionStage) by version id only.

Kubernetes Deployments (app/actions/k8s.py) get `k8s_rollout_undo` and `k8s_rollout_restart`
through the cluster's kubeconfig, on connections with the operate capability.
`EmergencyActionRouter` sends each plan to the implementation for its action.
"""

import hashlib
import json
import uuid
from datetime import datetime
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.policy.actions import ApprovedAction

ECS_ROLLBACK_SERVICE: Final = "ecs_rollback_service"
SECRET_ROLLBACK_VERSION: Final = "secret_rollback_version"  # noqa: S105 (an action name)
FORCE_REDEPLOY: Final = "force_redeploy"
K8S_ROLLOUT_UNDO: Final = "k8s_rollout_undo"
K8S_ROLLOUT_RESTART: Final = "k8s_rollout_restart"

EmergencyActionName = Literal[
    "ecs_rollback_service",
    "secret_rollback_version",
    "force_redeploy",
    "k8s_rollout_undo",
    "k8s_rollout_restart",
]
EMERGENCY_ACTION_NAMES: tuple[EmergencyActionName, ...] = (
    ECS_ROLLBACK_SERVICE,
    SECRET_ROLLBACK_VERSION,
    FORCE_REDEPLOY,
    K8S_ROLLOUT_UNDO,
    K8S_ROLLOUT_RESTART,
)
K8S_ACTIONS: frozenset[str] = frozenset({K8S_ROLLOUT_UNDO, K8S_ROLLOUT_RESTART})


class ActionPlan(BaseModel):
    """One emergency action, ready to approve. Shown on the approval card as is."""

    model_config = ConfigDict(frozen=True)

    action: EmergencyActionName
    connection_id: uuid.UUID
    # What it acts on: "checkout/checkout-worker" (cluster/service), the secret's ARN, or
    # "namespace/deployment".
    target: str
    # The exact call arguments: cluster and service, secret_arn, or namespace and deployment.
    params: dict[str, str]
    # State read by the plan, and the state after the action:
    #   ecs_rollback_service: task definition ARN now -> the previous revision's ARN
    #   secret_rollback_version: version id holding AWSCURRENT -> the one holding AWSPREVIOUS
    #   force_redeploy: the primary deployment id -> null (a new deployment of the same revision)
    #   k8s_rollout_undo: "<ns>/<name> revision <n>" now -> the previous revision
    #   k8s_rollout_restart: "<ns>/<name> revision <n>" -> null (new pods, same template)
    current: str | None
    desired: str | None
    # One line for the approval card, for example
    # "Point checkout-worker back at checkout-worker:41 (now checkout-worker:42)".
    summary: str
    planned_at: datetime

    def plan_hash(self) -> str:
        """Binds an approval to this plan. `planned_at` and `summary` are left out: two plans
        that make the same change hash the same."""
        body = {
            "action": self.action,
            "connection_id": str(self.connection_id),
            "target": self.target,
            "params": dict(sorted(self.params.items())),
            "current": self.current,
            "desired": self.desired,
        }
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class ActionResult(BaseModel):
    """What `execute` did. `ok=False` with `error` when AWS refused or the state moved."""

    action: EmergencyActionName
    ok: bool
    summary: str
    # Ids a person can check: new deployment id, task definition ARN, version ids.
    details: dict[str, str] = Field(default_factory=dict)
    error: str | None = None
    started_at: datetime
    finished_at: datetime


class EmergencyActionError(Exception):
    """A plan cannot be made (no previous revision, no AWSPREVIOUS version, the connection has no
    `operate` capability, or the resource is outside OperateServiceArns / OperateSecretArns).
    The message is safe to show and never holds a secret value."""


class EmergencyActions(Protocol):
    """Rizwan implements; Medic calls. All AWS calls go through `aws_operate_session`, which
    refuses unless the connection has the `operate` capability (stack parameter
    EnableOperate), and only reach OperateServiceArns / OperateSecretArns."""

    async def plan_ecs_rollback_service(
        self, connection_id: uuid.UUID, cluster: str, service: str
    ) -> ActionPlan:
        """Point the service back at its previous task definition revision."""
        ...

    async def plan_secret_rollback_version(
        self, connection_id: uuid.UUID, secret_arn: str
    ) -> ActionPlan:
        """Move AWSCURRENT back to the version holding AWSPREVIOUS (metadata only)."""
        ...

    async def plan_force_redeploy(
        self, connection_id: uuid.UUID, cluster: str, service: str
    ) -> ActionPlan:
        """Start new tasks of the same revision (they re-read secrets at start)."""
        ...

    async def plan_k8s_rollout_undo(
        self, connection_id: uuid.UUID, namespace: str, deployment: str
    ) -> ActionPlan:
        """Put the Deployment's previous revision's pod template back (kubectl rollout undo)."""
        ...

    async def plan_k8s_rollout_restart(
        self, connection_id: uuid.UUID, namespace: str, deployment: str
    ) -> ActionPlan:
        """Replace every pod with the same template (kubectl rollout restart)."""
        ...

    async def execute(
        self, plan: ActionPlan, token: ApprovedAction, *, run_id: uuid.UUID
    ) -> ActionResult:
        """Run an approved plan once. Raises PolicyViolationError for a missing, reused, or
        mismatched token; returns ok=False when the state moved since the plan."""
        ...


class AwsActions(Protocol):
    """The AWS half: what AwsEmergencyActions and FakeEmergencyActions implement."""

    async def plan_ecs_rollback_service(
        self, connection_id: uuid.UUID, cluster: str, service: str
    ) -> ActionPlan: ...

    async def plan_secret_rollback_version(
        self, connection_id: uuid.UUID, secret_arn: str
    ) -> ActionPlan: ...

    async def plan_force_redeploy(
        self, connection_id: uuid.UUID, cluster: str, service: str
    ) -> ActionPlan: ...

    async def execute(
        self, plan: ActionPlan, token: ApprovedAction, *, run_id: uuid.UUID
    ) -> ActionResult: ...


class K8sActions(Protocol):
    """The Kubernetes half (app.actions.k8s.K8sEmergencyActions)."""

    async def plan_k8s_rollout_undo(
        self, connection_id: uuid.UUID, namespace: str, deployment: str
    ) -> ActionPlan: ...

    async def plan_k8s_rollout_restart(
        self, connection_id: uuid.UUID, namespace: str, deployment: str
    ) -> ActionPlan: ...

    async def execute(
        self, plan: ActionPlan, token: ApprovedAction, *, run_id: uuid.UUID
    ) -> ActionResult: ...


class EmergencyActionRouter:
    """`EmergencyActions` from an AWS and a Kubernetes implementation: each plan and each
    execute goes to the one for its action. A missing half refuses with a plain reason."""

    def __init__(self, aws: AwsActions | None, k8s: K8sActions | None) -> None:
        self.aws, self.k8s = aws, k8s

    def _aws(self) -> AwsActions:
        if self.aws is None:
            raise EmergencyActionError("No AWS emergency actions here.")
        return self.aws

    def _k8s(self) -> K8sActions:
        if self.k8s is None:
            raise EmergencyActionError("No Kubernetes emergency actions here.")
        return self.k8s

    async def plan_ecs_rollback_service(
        self, connection_id: uuid.UUID, cluster: str, service: str
    ) -> ActionPlan:
        return await self._aws().plan_ecs_rollback_service(connection_id, cluster, service)

    async def plan_secret_rollback_version(
        self, connection_id: uuid.UUID, secret_arn: str
    ) -> ActionPlan:
        return await self._aws().plan_secret_rollback_version(connection_id, secret_arn)

    async def plan_force_redeploy(
        self, connection_id: uuid.UUID, cluster: str, service: str
    ) -> ActionPlan:
        return await self._aws().plan_force_redeploy(connection_id, cluster, service)

    async def plan_k8s_rollout_undo(
        self, connection_id: uuid.UUID, namespace: str, deployment: str
    ) -> ActionPlan:
        return await self._k8s().plan_k8s_rollout_undo(connection_id, namespace, deployment)

    async def plan_k8s_rollout_restart(
        self, connection_id: uuid.UUID, namespace: str, deployment: str
    ) -> ActionPlan:
        return await self._k8s().plan_k8s_rollout_restart(connection_id, namespace, deployment)

    async def execute(
        self, plan: ActionPlan, token: ApprovedAction, *, run_id: uuid.UUID
    ) -> ActionResult:
        half: AwsActions | K8sActions = self._k8s() if plan.action in K8S_ACTIONS else self._aws()
        return await half.execute(plan, token, run_id=run_id)


async def plan_again(actions: EmergencyActions, plan: ActionPlan) -> ActionPlan:
    """The same action on the same target, planned from the state now (a retry after a failed
    run gets a fresh plan and a fresh approval, never the old ones)."""
    p = plan.params
    if plan.action == K8S_ROLLOUT_UNDO:
        return await actions.plan_k8s_rollout_undo(
            plan.connection_id, p["namespace"], p["deployment"]
        )
    if plan.action == K8S_ROLLOUT_RESTART:
        return await actions.plan_k8s_rollout_restart(
            plan.connection_id, p["namespace"], p["deployment"]
        )
    if plan.action == ECS_ROLLBACK_SERVICE:
        return await actions.plan_ecs_rollback_service(
            plan.connection_id, p["cluster"], p["service"]
        )
    if plan.action == SECRET_ROLLBACK_VERSION:
        return await actions.plan_secret_rollback_version(plan.connection_id, p["secret_arn"])
    return await actions.plan_force_redeploy(plan.connection_id, p["cluster"], p["service"])
