"""Dev-only self-approve for live test scripts (Phase 4 live verification).

A live script (scripts/live_phase4.sh) may approve the deploy it started, without a person
clicking Approve, only when BOTH hold:

- APP_ENV=dev, and
- LIVE_SCRIPT_SELF_APPROVE=1.

Anything else (staging, production, the flag unset) refuses, and the route answers 404 as if
it did not exist. The decision is recorded like any other, with `decided_via =
"script:<name> (dev)"` instead of a user, so the inbox, the timeline, and the audit trail show
it was a script. It only approves: a script never rejects or decides for another workspace.
"""

import re
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Approval
from app.policy.engine import Decision, PolicyViolationError, decide_approval

_SCRIPT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def self_approve_allowed(settings: Settings) -> bool:
    return settings.app_env == "dev" and settings.live_script_self_approve


def script_label(script: str) -> str:
    if not _SCRIPT.fullmatch(script):
        raise PolicyViolationError(f"Not a script name: {script!r}.")
    return f"script:{script} (dev)"


async def self_approve(
    session: AsyncSession, approval_id: uuid.UUID, *, script: str, settings: Settings
) -> Approval:
    """Approve as `script:<name> (dev)`. Raises PolicyViolationError outside dev or without
    LIVE_SCRIPT_SELF_APPROVE=1."""
    if not self_approve_allowed(settings):
        raise PolicyViolationError(
            "Self-approve is only for live test scripts with APP_ENV=dev and "
            "LIVE_SCRIPT_SELF_APPROVE=1."
        )
    return await decide_approval(
        session, approval_id, Decision.approve, None, via=script_label(script)
    )
