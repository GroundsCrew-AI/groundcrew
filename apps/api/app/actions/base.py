"""Emergency actions on a customer's existing resources (docs/phase5-interfaces.md).

Rizwan owns this package and implements `EmergencyActions` on an Operate-tier session
(`CredentialsProvider.aws_operate_session`). Medic (Phase 5) only calls it. Until the real
implementation lands, Medic and its tests use `FakeEmergencyActions` (app/actions/fake.py).

Every action is two calls, like `Target.rollback_plan()` / `rollback()`:

1. `plan_*` reads the current state (no writes) and returns an `ActionPlan`: exactly what will
   change, from what to what. The plan is what a person approves (`plan_hash()` binds it).
2. `execute(plan, token)` runs that plan once. It refuses without an `ApprovedAction` for the
   same run, action, and plan hash (`app.policy.actions.consume_approved_action`, called before
   the first write), and refuses if the state moved since the plan (`current` no longer
   matches), so an approval never applies to a situation the approver did not see.

Secret values are never read: a secret rollback moves the AWSCURRENT stage back to the version
that holds AWSPREVIOUS (UpdateSecretVersionStage) by version id only.
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

EmergencyActionName = Literal["ecs_rollback_service", "secret_rollback_version", "force_redeploy"]
EMERGENCY_ACTION_NAMES: tuple[EmergencyActionName, ...] = (
    ECS_ROLLBACK_SERVICE,
    SECRET_ROLLBACK_VERSION,
    FORCE_REDEPLOY,
)


class ActionPlan(BaseModel):
    """One emergency action, ready to approve. Shown on the approval card as is."""

    model_config = ConfigDict(frozen=True)

    action: EmergencyActionName
    connection_id: uuid.UUID
    # What it acts on: "checkout/checkout-worker" (cluster/service) or the secret's ARN.
    target: str
    # The exact call arguments: cluster and service, or secret_arn.
    params: dict[str, str]
    # State read by the plan, and the state after the action:
    #   ecs_rollback_service: task definition ARN now -> the previous revision's ARN
    #   secret_rollback_version: version id holding AWSCURRENT -> the one holding AWSPREVIOUS
    #   force_redeploy: the primary deployment id -> null (a new deployment of the same revision)
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
    """A plan cannot be made (no previous revision, no AWSPREVIOUS version, not Operate tier).
    The message is safe to show and never holds a secret value."""


class EmergencyActions(Protocol):
    """Rizwan implements; Medic calls. All AWS calls go through `aws_operate_session` on a
    connection with tier=operate, in DISCOVERY_REGIONS."""

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

    async def execute(
        self, plan: ActionPlan, token: ApprovedAction, *, run_id: uuid.UUID
    ) -> ActionResult:
        """Run an approved plan once. Raises PolicyViolationError for a missing, reused, or
        mismatched token; returns ok=False when the state moved since the plan."""
        ...
