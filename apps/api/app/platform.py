"""Platform switches and sign-up limits: what an operator controls for every workspace.

- `signups_paused` (platform_settings): no new accounts, except for people with a pending
  invite. Existing users sign in as usual.
- At most SIGNUPS_PER_HOUR new accounts an hour in total, and SIGNUPS_PER_IP_DAY a day from one
  client address (in-process count, like the other rate limits).

`check_signup` runs right before a new user row is created (app.accounts), so only real new
accounts count. Operators change the switch with `python -m app.admin_cli signups pause|resume`
or in Settings (PLATFORM_ADMINS).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import PlatformSetting, User, WorkspaceInvite
from app.security.ratelimit import limiter

SIGNUPS_PAUSED = "signups_paused"
DAY = 86_400.0

SignupCode = Literal["signups_paused", "signup_rate_limited"]


class SignupRefusedError(Exception):
    def __init__(self, code: SignupCode, message: str) -> None:
        super().__init__(message)
        self.code: SignupCode = code


def is_platform_admin(user: User, settings: Settings) -> bool:
    admins = set(settings.platform_admins)
    return bool(admins) and (
        (
            user.email is not None
            and user.email_verified_at is not None
            and user.email.lower() in admins
        )
        or (user.github_login is not None and user.github_login.lower() in admins)
    )


async def signups_paused(session: AsyncSession) -> bool:
    row = await session.get(PlatformSetting, SIGNUPS_PAUSED)
    return bool(row is not None and (row.value or {}).get("on"))


async def set_signups_paused(session: AsyncSession, paused: bool, by: uuid.UUID | None) -> None:
    """The caller commits."""
    stmt = insert(PlatformSetting).values(key=SIGNUPS_PAUSED, value={"on": paused}, updated_by=by)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={"value": stmt.excluded.value, "updated_by": by, "updated_at": func.now()},
        )
    )


async def signups_last_hour(session: AsyncSession) -> int:
    since = datetime.now(UTC) - timedelta(hours=1)
    return int(
        await session.scalar(select(func.count(User.id)).where(User.created_at > since)) or 0
    )


async def _invited(session: AsyncSession, email: str | None, github_login: str | None) -> bool:
    targets = []
    if email:
        targets.append(WorkspaceInvite.email == email.lower())
    if github_login:
        targets.append(WorkspaceInvite.github_login == github_login.lower())
    if not targets:
        return False
    return (
        await session.scalar(select(WorkspaceInvite.id).where(or_(*targets)).limit(1)) is not None
    )


async def check_signup(
    session: AsyncSession,
    settings: Settings,
    *,
    ip: str | None,
    verified_email: str | None,
    github_login: str | None,
) -> None:
    """Raise SignupRefusedError unless a new account may be created now. An invite (to this
    verified address or GitHub login) gets through a pause, not the rate limits."""
    if await signups_paused(session) and not await _invited(session, verified_email, github_login):
        raise SignupRefusedError(
            "signups_paused",
            "New sign-ups are paused for now. If someone invited you, sign in with the address "
            "or GitHub account they invited.",
        )
    if await signups_last_hour(session) >= settings.signups_per_hour:
        raise SignupRefusedError(
            "signup_rate_limited", "Too many new accounts right now. Try again in an hour."
        )
    if ip and not limiter.allow(f"signup:ip:{ip}", settings.signups_per_ip_day, DAY):
        raise SignupRefusedError(
            "signup_rate_limited", "Too many new accounts from this address today."
        )
