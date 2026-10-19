"""Approved actions for deploy targets: the only way to call `Target.apply()` or `.rollback()`.

Tools go through `execute_tool`, which checks the approval itself. Targets are called by the
pipeline (Deployer, M5) and by Medic (Phase 5), not as tools, so they take an `ApprovedAction`
instead: a token that only `issue_approved_action` creates, from an approved, unused approval
for that run, action, and plan hash.

- **Signed with a persistent key** (APPROVAL_TOKEN_KEY; in APP_ENV=dev derived from SECRETS_KEY
  when unset), so a token issued by one worker is valid in another and after a restart, and
  code cannot make one by hand. `encode()` / `decode()` carry it through a job payload.
- **Single use in the database**: a target consumes the token before its first write with
  `UPDATE approvals SET used_at = now() WHERE id = ... AND used_at IS NULL`. Only one worker
  can win that update, so a token works exactly once however many workers hold it.

Approvals for targets use these tool names, and their `arguments` carry `plan_hash`:
- `target_apply`: policy `write`, the deploy plan.
- `target_rollback`: policy `emergency`, a rollback plan (also deleting a first deploy).

Medic (Phase 5, docs/phase5-interfaces.md) uses the same tokens:
- `open_fix_pr`: policy `write`, a fix pull request (`plan_hash` of the PR draft).
- `ecs_rollback_service`, `secret_rollback_version`, `force_redeploy`, `k8s_rollout_undo`,
  `k8s_rollout_restart`: policy `emergency`, an `app.actions.base.ActionPlan` (`plan_hash` of
  the plan).
"""

import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import Approval, ApprovalStatus, ToolPolicy
from app.db.session import SessionFactory
from app.policy.engine import PolicyViolationError

TARGET_APPLY: Final = "target_apply"
TARGET_ROLLBACK: Final = "target_rollback"
TargetAction = Literal["target_apply", "target_rollback"]
OPEN_FIX_PR: Final = "open_fix_pr"
MedicAction = Literal[
    "open_fix_pr",
    "ecs_rollback_service",
    "secret_rollback_version",
    "force_redeploy",
    "k8s_rollout_undo",
    "k8s_rollout_restart",
]
ApprovedActionName = TargetAction | MedicAction

# The policy each action's approval must have: a rollback (including deleting a first deploy)
# is an emergency action, never a plain write.
_POLICY: dict[str, ToolPolicy] = {
    TARGET_APPLY: ToolPolicy.write,
    TARGET_ROLLBACK: ToolPolicy.emergency,
    OPEN_FIX_PR: ToolPolicy.write,
    "ecs_rollback_service": ToolPolicy.emergency,
    "secret_rollback_version": ToolPolicy.emergency,
    "force_redeploy": ToolPolicy.emergency,
    "k8s_rollout_undo": ToolPolicy.emergency,
    "k8s_rollout_restart": ToolPolicy.emergency,
}


class ApprovalKeyError(PolicyViolationError):
    """No key to sign or check approved actions with (APPROVAL_TOKEN_KEY)."""


def _key(settings: Settings) -> bytes:
    if settings.approval_token_key is not None and settings.approval_token_key.get_secret_value():
        return settings.approval_token_key.get_secret_value().encode()
    if settings.app_env == "dev" and settings.secrets_key is not None:
        # Dev only: a key of its own, derived so it never equals SECRETS_KEY itself.
        secret = settings.secrets_key.get_secret_value().encode()
        return hmac.new(secret, b"groundcrew approval tokens", hashlib.sha256).digest()
    raise ApprovalKeyError(
        "APPROVAL_TOKEN_KEY is not set, so approved deploy actions cannot be signed or checked "
        "(required outside APP_ENV=dev; see .env.example)."
    )


def _seal(
    settings: Settings,
    approval_id: uuid.UUID,
    run_id: uuid.UUID,
    action: str,
    plan_hash: str,
    issued_at: datetime,
) -> str:
    message = f"{approval_id}|{run_id}|{action}|{plan_hash}|{issued_at.isoformat()}".encode()
    return hmac.new(_key(settings), message, hashlib.sha256).hexdigest()


class ApprovedAction(BaseModel):
    """Permission to run one approved plan once. Pass it to the target, or carry it to another
    worker with `encode()`; it is spent the first time a target consumes it."""

    model_config = ConfigDict(frozen=True)

    approval_id: uuid.UUID
    run_id: uuid.UUID
    action: ApprovedActionName
    plan_hash: str
    issued_at: datetime
    seal: str = Field(repr=False)

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode()

    @classmethod
    def decode(cls, text: str) -> "ApprovedAction":
        try:
            return cls.model_validate_json(base64.urlsafe_b64decode(text.encode()))
        except (ValueError, ValidationError, json.JSONDecodeError):
            raise PolicyViolationError("Not an approved action token.") from None


async def issue_approved_action(
    session: AsyncSession,
    approval: Approval,
    *,
    run_id: uuid.UUID,
    action: ApprovedActionName,
    plan_hash: str,
    settings: Settings | None = None,
) -> ApprovedAction:
    """A signed token for an approved, unused approval. Raises PolicyViolationError when the
    approval is not approved, was already used, or is for a different run, action, policy, or
    plan. Issuing writes nothing: single use is enforced when the token is consumed."""
    settings = settings or get_settings()
    stored = await session.get(Approval, approval.id, populate_existing=True)
    if stored is None:
        raise PolicyViolationError("Approval row no longer exists.")
    if stored.status != ApprovalStatus.approved:
        raise PolicyViolationError(f"Approval {stored.id} is {stored.status}, not approved.")
    if stored.used_at is not None or stored.executed_at is not None:
        raise PolicyViolationError(f"Approval {stored.id} was already used.")
    if stored.run_id != run_id:
        raise PolicyViolationError(f"Approval {stored.id} is for a different run.")
    if stored.tool_name != action or stored.policy != _POLICY[action]:
        raise PolicyViolationError(
            f"Approval {stored.id} is a {stored.policy} {stored.tool_name}, not {action}."
        )
    arguments = stored.arguments if isinstance(stored.arguments, dict) else {}
    if arguments.get("plan_hash") != plan_hash:
        raise PolicyViolationError(f"Approval {stored.id} was for a different plan.")
    issued_at = datetime.now(UTC)
    return ApprovedAction(
        approval_id=stored.id,
        run_id=run_id,
        action=action,
        plan_hash=plan_hash,
        issued_at=issued_at,
        seal=_seal(settings, stored.id, run_id, action, plan_hash, issued_at),
    )


async def consume_approved_action(
    session_factory: SessionFactory,
    token: object,
    *,
    run_id: uuid.UUID,
    action: ApprovedActionName,
    plan_hash: str,
    settings: Settings | None = None,
) -> None:
    """Called by a target before its first write. Raises PolicyViolationError unless `token`
    was issued for exactly this run, action, and plan, its approval is still approved, and no
    one used it before (checked and marked in one UPDATE, committed before any write)."""
    settings = settings or get_settings()
    if not isinstance(token, ApprovedAction):
        raise PolicyViolationError(f"{action} needs an approved action from the policy engine.")
    expected = _seal(
        settings, token.approval_id, token.run_id, token.action, token.plan_hash, token.issued_at
    )
    if not hmac.compare_digest(token.seal, expected):
        raise PolicyViolationError(
            f"{action}: this approved action was not issued by the policy engine."
        )
    if token.action != action:
        raise PolicyViolationError(f"{action}: the approved action is for {token.action}.")
    if token.run_id != run_id:
        raise PolicyViolationError(f"{action}: the approved action is for a different run.")
    if not hmac.compare_digest(token.plan_hash, plan_hash):
        raise PolicyViolationError(f"{action}: the approved action is for a different plan.")
    now = datetime.now(UTC)
    async with session_factory() as session:
        used = await session.scalar(
            update(Approval)
            .where(
                Approval.id == token.approval_id,
                Approval.run_id == run_id,
                Approval.tool_name == action,
                Approval.status == ApprovalStatus.approved,
                Approval.used_at.is_(None),
            )
            .values(used_at=now, executed_at=now)
            .returning(Approval.id)
        )
        await session.commit()
    if used is None:
        raise PolicyViolationError(
            f"{action}: approval {token.approval_id} was already used or is no longer approved."
        )
