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

Kubernetes Deployments and StatefulSets (app/actions/k8s.py) get `k8s_rollout_undo` and
`k8s_rollout_restart` through the cluster's kubeconfig, on connections with the operate
capability (`kind` "deployment" or "statefulset", see `k8s_params`).
Services a Groundcrew agent watches (app/actions/agent.py) get `agent_restart_service`: a
signed command the agent runs on its host, only if the service `allow`s restart there.
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
LAMBDA_ROLLBACK_ALIAS: Final = "lambda_rollback_alias"
EC2_REBOOT_INSTANCE: Final = "ec2_reboot_instance"
K8S_ROLLOUT_UNDO: Final = "k8s_rollout_undo"
K8S_ROLLOUT_RESTART: Final = "k8s_rollout_restart"
AGENT_RESTART_SERVICE: Final = "agent_restart_service"

EmergencyActionName = Literal[
    "ecs_rollback_service",
    "secret_rollback_version",
    "force_redeploy",
    "lambda_rollback_alias",
    "ec2_reboot_instance",
    "k8s_rollout_undo",
    "k8s_rollout_restart",
    "agent_restart_service",
]
EMERGENCY_ACTION_NAMES: tuple[EmergencyActionName, ...] = (
    ECS_ROLLBACK_SERVICE,
    SECRET_ROLLBACK_VERSION,
    FORCE_REDEPLOY,
    LAMBDA_ROLLBACK_ALIAS,
    EC2_REBOOT_INSTANCE,
    K8S_ROLLOUT_UNDO,
    K8S_ROLLOUT_RESTART,
    AGENT_RESTART_SERVICE,
)
K8S_ACTIONS: frozenset[str] = frozenset({K8S_ROLLOUT_UNDO, K8S_ROLLOUT_RESTART})
AGENT_ACTIONS: frozenset[str] = frozenset({AGENT_RESTART_SERVICE})
# The workload kinds the Kubernetes actions take (`kind`); deployment is the default.
K8S_WORKLOAD_KINDS: frozenset[str] = frozenset({"deployment", "statefulset"})


def k8s_params(namespace: str, name: str, kind: str = "deployment") -> dict[str, str]:
    """A Kubernetes action's params. A Deployment's stay {"namespace", "deployment"} (no
    `kind`), so plans and plan hashes made before StatefulSets existed stay valid; a
    StatefulSet's are {"namespace", "statefulset", "kind": "statefulset"}."""
    if kind not in K8S_WORKLOAD_KINDS:
        raise EmergencyActionError(f"Groundcrew does not act on Kubernetes {kind}s.")
    if kind == "deployment":
        return {"namespace": namespace, "deployment": name}
    return {"namespace": namespace, kind: name, "kind": kind}


def k8s_workload(params: dict[str, str]) -> tuple[str, str, str]:
    """(kind, namespace, name) from a Kubernetes action's params (`k8s_params`)."""
    kind = params.get("kind") or "deployment"
    if kind not in K8S_WORKLOAD_KINDS:
        raise EmergencyActionError(f"Groundcrew does not act on Kubernetes {kind}s.")
    return kind, params["namespace"], params[kind]


class ActionPlan(BaseModel):
    """One emergency action, ready to approve. Shown on the approval card as is."""

    model_config = ConfigDict(frozen=True)

    action: EmergencyActionName
    connection_id: uuid.UUID
    # What it acts on: "checkout/checkout-worker" (cluster/service), the secret's ARN, or
    # "namespace/name" (a Deployment or StatefulSet).
    target: str
    # The exact call arguments: cluster and service, secret_arn, or namespace and deployment
    # (a StatefulSet: namespace, statefulset, and kind="statefulset"; k8s_params).
    params: dict[str, str]
    # State read by the plan, and the state after the action:
    #   ecs_rollback_service: task definition ARN now -> the previous revision's ARN
    #   secret_rollback_version: version id holding AWSCURRENT -> the one holding AWSPREVIOUS
    #   force_redeploy: the primary deployment id -> null (a new deployment of the same revision)
    #   lambda_rollback_alias: "<function ARN>:<version>" the alias is on -> the previous version
    #   ec2_reboot_instance: "<id> running, system ..., instance ..." -> null (a reboot)
    #   k8s_rollout_undo: "<ns>/<name> revision <n>" now -> the previous revision
    #   k8s_rollout_restart: "<ns>/<name> revision <n>" -> null (new pods, same template)
    #   agent_restart_service: "<service> <state>" as the agent reported it -> null (a restart)
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

    async def plan_lambda_rollback_alias(
        self, connection_id: uuid.UUID, function_arn: str
    ) -> ActionPlan:
        """Point the function's alias back at its previous published version."""
        ...

    async def plan_ec2_reboot_instance(
        self, connection_id: uuid.UUID, instance_arn: str
    ) -> ActionPlan:
        """Reboot a running instance whose status check is impaired."""
        ...

    async def plan_k8s_rollout_undo(
        self, connection_id: uuid.UUID, namespace: str, name: str, kind: str = "deployment"
    ) -> ActionPlan:
        """Put the workload's previous revision's pod template back (kubectl rollout undo).
        `kind` is "deployment" (the default) or "statefulset"."""
        ...

    async def plan_k8s_rollout_restart(
        self, connection_id: uuid.UUID, namespace: str, name: str, kind: str = "deployment"
    ) -> ActionPlan:
        """Replace every pod with the same template (kubectl rollout restart)."""
        ...

    async def plan_agent_restart_service(
        self, connection_id: uuid.UUID, service: str
    ) -> ActionPlan:
        """Restart a service a Groundcrew agent watches (its configuration must allow it)."""
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

    async def plan_lambda_rollback_alias(
        self, connection_id: uuid.UUID, function_arn: str
    ) -> ActionPlan: ...

    async def plan_ec2_reboot_instance(
        self, connection_id: uuid.UUID, instance_arn: str
    ) -> ActionPlan: ...

    async def execute(
        self, plan: ActionPlan, token: ApprovedAction, *, run_id: uuid.UUID
    ) -> ActionResult: ...


class K8sActions(Protocol):
    """The Kubernetes half (app.actions.k8s.K8sEmergencyActions)."""

    async def plan_k8s_rollout_undo(
        self, connection_id: uuid.UUID, namespace: str, name: str, kind: str = "deployment"
    ) -> ActionPlan: ...

    async def plan_k8s_rollout_restart(
        self, connection_id: uuid.UUID, namespace: str, name: str, kind: str = "deployment"
    ) -> ActionPlan: ...

    async def execute(
        self, plan: ActionPlan, token: ApprovedAction, *, run_id: uuid.UUID
    ) -> ActionResult: ...


class AgentActions(Protocol):
    """Services on hosts with a Groundcrew agent (app.actions.agent.AgentEmergencyActions)."""

    async def plan_agent_restart_service(
        self, connection_id: uuid.UUID, service: str
    ) -> ActionPlan: ...

    async def execute(
        self, plan: ActionPlan, token: ApprovedAction, *, run_id: uuid.UUID
    ) -> ActionResult: ...


class EmergencyActionRouter:
    """`EmergencyActions` from AWS, Kubernetes, and agent implementations: each plan and each
    execute goes to the one for its action. A missing one refuses with a plain reason."""

    def __init__(
        self, aws: AwsActions | None, k8s: K8sActions | None, agent: AgentActions | None = None
    ) -> None:
        self.aws, self.k8s, self.agent = aws, k8s, agent

    def _aws(self) -> AwsActions:
        if self.aws is None:
            raise EmergencyActionError("No AWS emergency actions here.")
        return self.aws

    def _k8s(self) -> K8sActions:
        if self.k8s is None:
            raise EmergencyActionError("No Kubernetes emergency actions here.")
        return self.k8s

    def _agent(self) -> AgentActions:
        if self.agent is None:
            raise EmergencyActionError("No agent emergency actions here.")
        return self.agent

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

    async def plan_lambda_rollback_alias(
        self, connection_id: uuid.UUID, function_arn: str
    ) -> ActionPlan:
        return await self._aws().plan_lambda_rollback_alias(connection_id, function_arn)

    async def plan_ec2_reboot_instance(
        self, connection_id: uuid.UUID, instance_arn: str
    ) -> ActionPlan:
        return await self._aws().plan_ec2_reboot_instance(connection_id, instance_arn)

    async def plan_k8s_rollout_undo(
        self, connection_id: uuid.UUID, namespace: str, name: str, kind: str = "deployment"
    ) -> ActionPlan:
        return await self._k8s().plan_k8s_rollout_undo(connection_id, namespace, name, kind)

    async def plan_k8s_rollout_restart(
        self, connection_id: uuid.UUID, namespace: str, name: str, kind: str = "deployment"
    ) -> ActionPlan:
        return await self._k8s().plan_k8s_rollout_restart(connection_id, namespace, name, kind)

    async def plan_agent_restart_service(
        self, connection_id: uuid.UUID, service: str
    ) -> ActionPlan:
        return await self._agent().plan_agent_restart_service(connection_id, service)

    async def execute(
        self, plan: ActionPlan, token: ApprovedAction, *, run_id: uuid.UUID
    ) -> ActionResult:
        half: AwsActions | K8sActions | AgentActions = (
            self._k8s()
            if plan.action in K8S_ACTIONS
            else self._agent()
            if plan.action in AGENT_ACTIONS
            else self._aws()
        )
        return await half.execute(plan, token, run_id=run_id)


async def plan_again(actions: EmergencyActions, plan: ActionPlan) -> ActionPlan:
    """The same action on the same target, planned from the state now (a retry after a failed
    run gets a fresh plan and a fresh approval, never the old ones)."""
    p = plan.params
    if plan.action == AGENT_RESTART_SERVICE:
        return await actions.plan_agent_restart_service(plan.connection_id, p["service"])
    if plan.action == K8S_ROLLOUT_UNDO:
        kind, namespace, name = k8s_workload(p)
        return await actions.plan_k8s_rollout_undo(plan.connection_id, namespace, name, kind)
    if plan.action == K8S_ROLLOUT_RESTART:
        kind, namespace, name = k8s_workload(p)
        return await actions.plan_k8s_rollout_restart(plan.connection_id, namespace, name, kind)
    if plan.action == LAMBDA_ROLLBACK_ALIAS:
        return await actions.plan_lambda_rollback_alias(plan.connection_id, p["function_arn"])
    if plan.action == EC2_REBOOT_INSTANCE:
        return await actions.plan_ec2_reboot_instance(plan.connection_id, p["instance_arn"])
    if plan.action == ECS_ROLLBACK_SERVICE:
        return await actions.plan_ecs_rollback_service(
            plan.connection_id, p["cluster"], p["service"]
        )
    if plan.action == SECRET_ROLLBACK_VERSION:
        return await actions.plan_secret_rollback_version(plan.connection_id, p["secret_arn"])
    return await actions.plan_force_redeploy(plan.connection_id, p["cluster"], p["service"])
