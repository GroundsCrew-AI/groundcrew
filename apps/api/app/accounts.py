"""Users, sign-in, and workspace access.

A user signs in with a provider account (GitHub, Google: user_identities) or with their email
and a password. The web server calls these through /auth (app/routes/auth.py) and then sends
the user's id on every request; nothing is created per request.

Linking: a provider account whose email the provider verified joins the user with that
verified address. If that user never confirmed the address (someone signed up with it and
never opened the link), the provider proves who owns it: the password is removed and old
sessions end, so whoever squatted on the address loses it.

Password accounts confirm their address before they can sign in (AUTH_REQUIRE_VERIFIED_EMAIL),
and confirming needs the password too, so clicking a link someone else triggered does nothing.
A new user gets a workspace of their own when the account becomes usable: at the first
provider sign-in, or when a password account confirms its address.

Workspace access always goes through `member_workspace` / `require_member`, which raise
NotFoundError for workspaces the user is not a member of: a 404, never a 403, so other
workspaces' ids cannot be probed. Both take the lowest role the action needs (`minimum`):
viewers read, approvers also start work and decide approvals, owners also manage members,
connections, and settings (`require_owner`). A member below it gets ForbiddenError (403):
they can see the workspace, so a 403 leaks nothing.

Invites (by email or GitHub login) become memberships when that person signs in with a
verified address or that GitHub account.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    AuthToken,
    AuthTokenPurpose,
    User,
    UserIdentity,
    Workspace,
    WorkspaceInvite,
    WorkspaceMember,
    WorkspaceRole,
)
from app.platform import check_signup
from app.security.passwords import hash_password_async, needs_rehash, verify_password_async


class NotFoundError(Exception):
    """Does not exist, or the user may not see it. Maps to 404."""


class ForbiddenError(Exception):
    """A member tried an action their role does not allow. Maps to 403."""


# Each role can do everything the roles below it can.
ROLE_RANK: dict[WorkspaceRole, int] = {
    WorkspaceRole.viewer: 0,
    WorkspaceRole.approver: 1,
    WorkspaceRole.owner: 2,
}
_NEEDS = {
    WorkspaceRole.approver: "Viewers can look but not change anything. Ask a workspace owner for "
    "the approver role.",
    WorkspaceRole.owner: "Only a workspace owner can do this.",
}


def role_allows(role: WorkspaceRole, minimum: WorkspaceRole) -> bool:
    return ROLE_RANK[role] >= ROLE_RANK[minimum]


def check_role(role: WorkspaceRole, minimum: WorkspaceRole) -> None:
    if not role_allows(role, minimum):
        raise ForbiddenError(_NEEDS[minimum])


SignInCode = Literal["invalid", "unverified", "locked"]


class SignInError(Exception):
    def __init__(self, code: SignInCode) -> None:
        super().__init__(code)
        self.code: SignInCode = code


class LinkError(Exception):
    """An email link that is unknown, used, expired, or for an address the user no longer has."""


class OAuthProfile(BaseModel):
    """What the web server learned from the provider at sign-in."""

    provider: Literal["github", "google"]
    account_id: str = Field(min_length=1, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    # Only a verified address links accounts or applies email invites.
    email_verified: bool = False
    # GitHub login, for display and invites by login.
    login: str | None = Field(default=None, max_length=100)
    name: str | None = Field(default=None, max_length=200)
    avatar_url: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _github_id_is_numeric(self) -> "OAuthProfile":
        if self.provider == "github" and not self.account_id.isdigit():
            raise ValueError("A GitHub account id is a number.")
        return self


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _now() -> datetime:
    return datetime.now(UTC)


def display_name(user: User) -> str:
    return user.name or user.github_login or (user.email or "").partition("@")[0] or "Someone"


async def user_by_email(session: AsyncSession, email: str) -> User | None:
    user: User | None = await session.scalar(
        select(User).where(func.lower(User.email) == normalize_email(email))
    )
    return user


async def apply_invites(session: AsyncSession, user: User) -> None:
    """Turn this user's pending invites (their GitHub login, their verified email) into
    memberships. The caller commits."""
    targets = []
    if user.github_login:
        targets.append(WorkspaceInvite.github_login == user.github_login.lower())
    if user.email and user.email_verified_at is not None:
        targets.append(WorkspaceInvite.email == normalize_email(user.email))
    if not targets:
        return
    invites = list(await session.scalars(select(WorkspaceInvite).where(or_(*targets))))
    for invite in invites:
        exists = await session.scalar(
            select(WorkspaceMember.id).where(
                WorkspaceMember.workspace_id == invite.workspace_id,
                WorkspaceMember.user_id == user.id,
            )
        )
        if exists is None:
            session.add(
                WorkspaceMember(workspace_id=invite.workspace_id, user_id=user.id, role=invite.role)
            )
            await session.flush()
        await session.delete(invite)
    if invites:
        await session.flush()


async def _activate(session: AsyncSession, user: User) -> None:
    """A usable account's own workspace (once: only while it is in none), then its invites."""
    member_of = await session.scalar(
        select(func.count(WorkspaceMember.id)).where(WorkspaceMember.user_id == user.id)
    )
    if not member_of:
        workspace = Workspace(name=f"{display_name(user)}'s workspace", owner_id=user.id)
        session.add(workspace)
        await session.flush()
        session.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.owner)
        )
        await session.flush()
    await apply_invites(session, user)


async def _sync_github(session: AsyncSession, user: User, profile: OAuthProfile) -> None:
    """Keep users.github_id / github_login in step with the GitHub identity. A login another
    user still holds is stale (GitHub logins are unique), so it moves."""
    user.github_id = int(profile.account_id)
    login = profile.login or None
    if login and user.github_login != login:
        await session.execute(
            update(User)
            .where(func.lower(User.github_login) == login.lower(), User.id != user.id)
            .values(github_login=None)
        )
    user.github_login = login


def _apply_profile(user: User, profile: OAuthProfile) -> None:
    if profile.name and not user.name:
        user.name = profile.name
    if profile.avatar_url:
        user.avatar_url = profile.avatar_url


async def _oauth_sign_in(
    session: AsyncSession, profile: OAuthProfile, settings: Settings | None, ip: str | None
) -> User:
    email = normalize_email(profile.email) if profile.email else None
    verified = bool(email and profile.email_verified)
    now = _now()
    identity = await session.scalar(
        select(UserIdentity).where(
            UserIdentity.provider == profile.provider,
            UserIdentity.provider_account_id == profile.account_id,
        )
    )
    if identity is not None:
        user = await session.get(User, identity.user_id)
        if user is None:  # pragma: no cover - the foreign key cascades
            raise LinkError("Identity without a user.")
        identity.email, identity.login = email, profile.login
        if profile.provider == "github":
            await _sync_github(session, user, profile)
        _apply_profile(user, profile)
        same_address = normalize_email(user.email or "") == email
        if verified and same_address and user.email_verified_at is None:
            user.email_verified_at = now
        await apply_invites(session, user)
        return user

    user = None
    if verified and email:
        user = await user_by_email(session, email)
        if user is not None:
            same_provider = await session.scalar(
                select(UserIdentity.id).where(
                    UserIdentity.user_id == user.id, UserIdentity.provider == profile.provider
                )
            )
            if same_provider is not None:
                user = None  # another account of this provider has the address: keep apart
            elif user.email_verified_at is None:
                # Whoever signed up with this address never proved it; the provider has.
                user.password_hash = None
                user.sessions_valid_after = now
                user.email_verified_at = now
    created = user is None
    if user is None:
        if settings is not None:
            await check_signup(
                session,
                settings,
                ip=ip,
                verified_email=email if verified else None,
                github_login=profile.login if profile.provider == "github" else None,
            )
        taken = email is not None and await user_by_email(session, email) is not None
        user = User(
            email=None if taken else email,
            email_verified_at=now if verified and not taken else None,
            name=profile.name or profile.login,
            avatar_url=profile.avatar_url,
        )
        session.add(user)
        await session.flush()
    session.add(
        UserIdentity(
            user_id=user.id,
            provider=profile.provider,
            provider_account_id=profile.account_id,
            email=email,
            login=profile.login,
        )
    )
    if profile.provider == "github":
        await _sync_github(session, user, profile)
    _apply_profile(user, profile)
    await session.flush()
    if created:
        await _activate(session, user)
    else:
        await apply_invites(session, user)
    return user


async def oauth_sign_in(
    session: AsyncSession,
    profile: OAuthProfile,
    *,
    settings: Settings | None = None,
    ip: str | None = None,
) -> User:
    """The user for this provider account: found, linked by verified email, or created with a
    workspace. With `settings`, a new account must pass the sign-up switch and limits
    (app.platform.SignupRefusedError). Commits."""
    try:
        user = await _oauth_sign_in(session, profile, settings, ip)
        await session.commit()
        return user
    except IntegrityError:
        # Two first sign-ins raced; the other one created the rows.
        await session.rollback()
        user = await _oauth_sign_in(session, profile, None, ip)
        await session.commit()
        return user


async def password_sign_up(
    session: AsyncSession,
    email: str,
    password_hash: str,
    name: str | None,
    *,
    settings: Settings | None = None,
    ip: str | None = None,
) -> tuple[User, bool]:
    """(user, created). An address that already has an account is not changed: the caller
    tells its owner by email instead, so nobody learns here which addresses exist. A new
    account must pass the sign-up switch and limits (with `settings`)."""
    email = normalize_email(email)
    existing = await user_by_email(session, email)
    if existing is not None:
        return existing, False
    if settings is not None:
        # The address is not proven yet, so an email invite does not get through a pause.
        await check_signup(session, settings, ip=ip, verified_email=None, github_login=None)
    user = User(email=email, name=name, password_hash=password_hash)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await user_by_email(session, email)
        if existing is None:
            raise
        return existing, False
    return user, True


async def password_sign_in(
    session: AsyncSession, email: str, password: str, settings: Settings
) -> User:
    """The user for this email and password, or SignInError. Wrong passwords count towards a
    lockout; an unknown address takes as long as a wrong password. Commits."""
    user = await user_by_email(session, email)
    now = _now()
    if user is not None and user.locked_until is not None and user.locked_until > now:
        await verify_password_async(password, None)
        raise SignInError("locked")
    ok = await verify_password_async(password, user.password_hash if user else None)
    if user is None:
        raise SignInError("invalid")
    if not ok:
        user.failed_sign_ins += 1
        if user.failed_sign_ins >= settings.auth_lockout_attempts:
            user.locked_until = now + timedelta(minutes=settings.auth_lockout_minutes)
            user.failed_sign_ins = 0
        await session.commit()
        raise SignInError("invalid")
    user.failed_sign_ins, user.locked_until = 0, None
    if user.password_hash and needs_rehash(user.password_hash):
        user.password_hash = await hash_password_async(password)
    if settings.auth_require_verified_email and user.email_verified_at is None:
        await session.commit()
        raise SignInError("unverified")
    await apply_invites(session, user)
    await session.commit()
    return user


# --- email links -----------------------------------------------------------------------------


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def links_sent_recently(
    session: AsyncSession, user: User, purpose: AuthTokenPurpose, within: timedelta
) -> int:
    return int(
        await session.scalar(
            select(func.count(AuthToken.id)).where(
                AuthToken.user_id == user.id,
                AuthToken.purpose == purpose.value,
                AuthToken.created_at > _now() - within,
            )
        )
        or 0
    )


async def issue_link(
    session: AsyncSession, user: User, purpose: AuthTokenPurpose, ttl: timedelta
) -> str:
    """A new single-use token for `user.email` (the caller commits and sends it)."""
    if not user.email:
        raise LinkError("The account has no email address.")
    raw = secrets.token_urlsafe(32)
    session.add(
        AuthToken(
            user_id=user.id,
            purpose=purpose.value,
            token_hash=_token_hash(raw),
            email=normalize_email(user.email),
            expires_at=_now() + ttl,
        )
    )
    await session.flush()
    return raw


async def links_sent_to(
    session: AsyncSession, email: str, purpose: AuthTokenPurpose, within: timedelta
) -> int:
    """Links of this kind sent to this address recently (accounts or not)."""
    return int(
        await session.scalar(
            select(func.count(AuthToken.id)).where(
                AuthToken.email == normalize_email(email),
                AuthToken.purpose == purpose.value,
                AuthToken.created_at > _now() - within,
            )
        )
        or 0
    )


async def issue_sign_in_link(session: AsyncSession, email: str, ttl: timedelta) -> str:
    """A single-use sign-in link token for `email`, whether or not it has an account yet. The
    caller commits and sends it."""
    email = normalize_email(email)
    user = await user_by_email(session, email)
    raw = secrets.token_urlsafe(32)
    session.add(
        AuthToken(
            user_id=user.id if user else None,
            purpose=AuthTokenPurpose.sign_in.value,
            token_hash=_token_hash(raw),
            email=email,
            expires_at=_now() + ttl,
        )
    )
    await session.flush()
    return raw


async def email_link_sign_in(
    session: AsyncSession, raw: str, *, settings: Settings | None = None, ip: str | None = None
) -> User:
    """Sign in with an emailed link: the link proves the address. The account with that
    address (same verified email = same user), or a new one with a workspace (after the
    sign-up checks). Every other unused sign-in link to the address stops working. Commits."""
    row = await session.scalar(
        select(AuthToken)
        .where(
            AuthToken.token_hash == _token_hash(raw),
            AuthToken.purpose == AuthTokenPurpose.sign_in.value,
        )
        .with_for_update()
    )
    now = _now()
    if row is None or row.used_at is not None or row.expires_at <= now:
        raise LinkError("This link is invalid or has expired.")
    email = row.email
    await session.execute(
        update(AuthToken)
        .where(
            AuthToken.email == email,
            AuthToken.purpose == AuthTokenPurpose.sign_in.value,
            AuthToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    user = await user_by_email(session, email)
    if user is None:
        if settings is not None:
            await check_signup(session, settings, ip=ip, verified_email=email, github_login=None)
        user = User(email=email, email_verified_at=now)
        session.add(user)
        await session.flush()
        await _activate(session, user)
    elif user.email_verified_at is None:
        # Whoever signed up with this address never proved it; this link does.
        user.password_hash = None
        user.sessions_valid_after = now
        user.email_verified_at = now
        user.failed_sign_ins, user.locked_until = 0, None
        await _activate(session, user)
    else:
        await apply_invites(session, user)
    await session.commit()
    return user


async def _open_link(
    session: AsyncSession, raw: str, purpose: AuthTokenPurpose
) -> tuple[AuthToken, User]:
    row = await session.scalar(
        select(AuthToken)
        .where(AuthToken.token_hash == _token_hash(raw), AuthToken.purpose == purpose.value)
        .with_for_update()
    )
    if row is None or row.used_at is not None or row.expires_at <= _now() or row.user_id is None:
        raise LinkError("This link is invalid or has expired.")
    user = await session.get(User, row.user_id)
    if user is None or normalize_email(user.email or "") != row.email:
        raise LinkError("This link is invalid or has expired.")
    return row, user


async def _confirm_email(session: AsyncSession, user: User) -> None:
    if user.email_verified_at is None:
        user.email_verified_at = _now()
        await _activate(session, user)
    else:
        await apply_invites(session, user)


async def verify_email(session: AsyncSession, raw: str, password: str) -> User:
    """Confirm the address from the link. The account's password is needed as well, so a link
    someone else's sign-up sent you cannot verify their account. Commits."""
    row, user = await _open_link(session, raw, AuthTokenPurpose.verify_email)
    if user.password_hash is not None and not await verify_password_async(
        password, user.password_hash
    ):
        await session.rollback()
        raise SignInError("invalid")
    row.used_at = _now()
    user.failed_sign_ins, user.locked_until = 0, None
    await _confirm_email(session, user)
    await session.commit()
    return user


async def reset_password(session: AsyncSession, raw: str, password_hash: str) -> User:
    """Set a new password from a reset link. The link proves the address, so it confirms it;
    every other session ends. Commits."""
    row, user = await _open_link(session, raw, AuthTokenPurpose.reset_password)
    now = _now()
    await session.execute(
        update(AuthToken)
        .where(
            AuthToken.user_id == user.id,
            AuthToken.purpose == AuthTokenPurpose.reset_password.value,
            AuthToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    row.used_at = now
    user.password_hash = password_hash
    user.sessions_valid_after = now
    user.failed_sign_ins, user.locked_until = 0, None
    await _confirm_email(session, user)
    await session.commit()
    return user


async def change_password(
    session: AsyncSession, user: User, current: str | None, password_hash: str
) -> None:
    """Change (or, for a GitHub or Google account, add) the password. Ends every session,
    this one included. SignInError("invalid") when the current password is wrong. Commits."""
    if user.password_hash is not None and not await verify_password_async(
        current or "", user.password_hash
    ):
        raise SignInError("invalid")
    user.password_hash = password_hash
    user.sessions_valid_after = _now()
    await session.commit()


async def sign_in_methods(session: AsyncSession, user: User) -> list[str]:
    providers = list(
        await session.scalars(
            select(UserIdentity.provider)
            .where(UserIdentity.user_id == user.id)
            .order_by(UserIdentity.provider)
        )
    )
    return (["password"] if user.password_hash else []) + providers


# --- workspace access ------------------------------------------------------------------------


async def member_workspace(
    session: AsyncSession,
    user: User,
    workspace_id: uuid.UUID,
    minimum: WorkspaceRole = WorkspaceRole.viewer,
) -> tuple[Workspace, WorkspaceRole]:
    row = (
        await session.execute(
            select(Workspace, WorkspaceMember.role)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .where(Workspace.id == workspace_id, WorkspaceMember.user_id == user.id)
        )
    ).first()
    if row is None:
        raise NotFoundError("Workspace not found.")
    check_role(row[1], minimum)
    return row[0], row[1]


async def require_owner(session: AsyncSession, user: User, workspace_id: uuid.UUID) -> Workspace:
    """The workspace when the user owns it. NotFoundError for non-members, ForbiddenError for
    members who are not owners."""
    workspace, _ = await member_workspace(session, user, workspace_id, WorkspaceRole.owner)
    return workspace


async def member_role(
    session: AsyncSession, user_id: uuid.UUID, workspace_id: uuid.UUID
) -> WorkspaceRole | None:
    role: WorkspaceRole | None = await session.scalar(
        select(WorkspaceMember.role).where(
            WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id
        )
    )
    return role


async def require_member(
    session: AsyncSession,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    minimum: WorkspaceRole = WorkspaceRole.viewer,
) -> None:
    role = await member_role(session, user_id, workspace_id)
    if role is None:
        raise NotFoundError("Not found.")
    check_role(role, minimum)


async def user_workspaces(
    session: AsyncSession, user: User
) -> list[tuple[Workspace, WorkspaceRole]]:
    rows = await session.execute(
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user.id)
        .order_by(Workspace.created_at)
    )
    return [(w, r) for w, r in rows]
