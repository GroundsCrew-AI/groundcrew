"""Approved actions for deploy targets: the only way to call `Target.apply()` or `.rollback()`.

Tools go through `execute_tool`, which checks the approval itself. Targets are called by the
pipeline (Deployer, M5) and by Medic (Phase 5), not as tools, so they take an `ApprovedAction`
instead: a single-use token that only `issue_approved_action` creates, from an approved,
unused approval for that run, action, and plan hash. Issuing marks the approval executed
(committed before anything runs), so an approval yields at most one token; a target consumes
the token before its first write, so a token works at most once.

The token is sealed with a key that lives only in this process, so code cannot build one by
hand: an `ApprovedAction(...)` with a made-up seal is refused. Workers issue and consume in
the same process, right before the call.

Approvals for targets use these tool names, and their `arguments` carry `plan_hash`:
- `target_apply`: policy `write`, the deploy plan.
- `target_rollback`: policy `emergency`, a rollback plan (also deleting a first deploy).
"""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Approval, ApprovalStatus, ToolPolicy
from app.policy.engine import PolicyViolationError

TARGET_APPLY: Final = "target_apply"
TARGET_ROLLBACK: Final = "target_rollback"
TargetAction = Literal["target_apply", "target_rollback"]

# The policy each action's approval must have: a rollback (including deleting a first deploy)
# is an emergency action, never a plain write.
_POLICY: dict[str, ToolPolicy] = {
    TARGET_APPLY: ToolPolicy.write,
    TARGET_ROLLBACK: ToolPolicy.emergency,
}

_KEY = secrets.token_bytes(32)
# Approval ids whose token was consumed in this process (a token works once).
_consumed: set[uuid.UUID] = set()


def _seal(approval_id: uuid.UUID, run_id: uuid.UUID, action: str, plan_hash: str) -> str:
    message = f"{approval_id}|{run_id}|{action}|{plan_hash}".encode()
    return hmac.new(_KEY, message, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class ApprovedAction:
    """Permission to run one approved plan once. Pass it to the target; never store it."""

    approval_id: uuid.UUID
    run_id: uuid.UUID
    action: TargetAction
    plan_hash: str
    seal: str = field(repr=False)


async def issue_approved_action(
    session: AsyncSession,
    approval: Approval,
    *,
    run_id: uuid.UUID,
    action: TargetAction,
    plan_hash: str,
) -> ApprovedAction:
    """Turns an approved, unused approval into a token, and marks the approval used.

    Raises PolicyViolationError when the approval is not approved, was already used, or is for
    a different run, action, policy, or plan."""
    locked = await session.get(Approval, approval.id, with_for_update=True, populate_existing=True)
    if locked is None:
        raise PolicyViolationError("Approval row no longer exists.")
    if locked.status != ApprovalStatus.approved:
        raise PolicyViolationError(f"Approval {locked.id} is {locked.status}, not approved.")
    if locked.executed_at is not None:
        raise PolicyViolationError(f"Approval {locked.id} was already used.")
    if locked.run_id != run_id:
        raise PolicyViolationError(f"Approval {locked.id} is for a different run.")
    if locked.tool_name != action or locked.policy != _POLICY[action]:
        raise PolicyViolationError(
            f"Approval {locked.id} is a {locked.policy} {locked.tool_name}, not {action}."
        )
    arguments = locked.arguments if isinstance(locked.arguments, dict) else {}
    if arguments.get("plan_hash") != plan_hash:
        raise PolicyViolationError(f"Approval {locked.id} was for a different plan.")
    # Committed before anything runs: an approval is spent even if the action then crashes.
    locked.executed_at = datetime.now(UTC)
    await session.commit()
    return ApprovedAction(
        approval_id=locked.id,
        run_id=run_id,
        action=action,
        plan_hash=plan_hash,
        seal=_seal(locked.id, run_id, action, plan_hash),
    )


def consume_approved_action(
    token: object, *, run_id: uuid.UUID, action: TargetAction, plan_hash: str
) -> None:
    """Called by a target before its first write. Raises PolicyViolationError unless `token`
    was issued here for exactly this run, action, and plan, and was not consumed before."""
    if not isinstance(token, ApprovedAction):
        raise PolicyViolationError(f"{action} needs an approved action from the policy engine.")
    expected = _seal(token.approval_id, token.run_id, token.action, token.plan_hash)
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
    if token.approval_id in _consumed:
        raise PolicyViolationError(f"{action}: approval {token.approval_id} was already used.")
    _consumed.add(token.approval_id)
