"""Users and workspace access.

Users are upserted by GitHub id on every authenticated request (written only when something
changed). On a user's first login they get a workspace named "<login>'s workspace".

Workspace access always goes through `member_workspace` / `require_member`, which raise
NotFoundError for workspaces the user is not a member of: a 404, never a 403, so other
workspaces' ids cannot be probed.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User, Workspace, WorkspaceMember, WorkspaceRole
from app.security.auth import WebIdentity


class NotFoundError(Exception):
    """Does not exist, or the user may not see it. Maps to 404."""


async def _by_github_id(session: AsyncSession, github_id: int) -> User | None:
    user: User | None = await session.scalar(select(User).where(User.github_id == github_id))
    return user


async def _email_taken(session: AsyncSession, email: str, github_id: int) -> bool:
    other = await session.scalar(
        select(User.id).where(User.email == email, User.github_id.is_distinct_from(github_id))
    )
    return other is not None


async def _create_user(session: AsyncSession, identity: WebIdentity) -> User:
    email = identity.email
    if email and await _email_taken(session, email, identity.github_id):
        email = None  # someone else already uses it; email is informational only
    user = User(
        github_id=identity.github_id,
        github_login=identity.login,
        email=email,
        name=identity.name,
        avatar_url=identity.avatar_url,
    )
    session.add(user)
    await session.flush()
    workspace = Workspace(name=f"{identity.login}'s workspace", owner_id=user.id)
    session.add(workspace)
    await session.flush()
    session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.owner)
    )
    await session.flush()
    return user


async def upsert_user(session: AsyncSession, identity: WebIdentity) -> User:
    """The user for this GitHub identity, created with a workspace on first login. Commits."""
    user = await _by_github_id(session, identity.github_id)
    if user is None:
        try:
            user = await _create_user(session, identity)
            await session.commit()
            return user
        except IntegrityError:
            # Two first requests raced, or a stale login is held by another account.
            await session.rollback()
            user = await _by_github_id(session, identity.github_id)
            if user is None:
                raise

    for field, value in (
        ("github_login", identity.login),
        ("name", identity.name),
        ("avatar_url", identity.avatar_url),
    ):
        if getattr(user, field) != value:
            setattr(user, field, value)
    if (
        identity.email
        and user.email != identity.email
        and not await _email_taken(session, identity.email, identity.github_id)
    ):
        user.email = identity.email
    # Commit either way to end the transaction. (A rollback would expire `user`, and the
    # caller would then lazy-load it outside the async context.)
    await session.commit()
    return user


async def member_workspace(
    session: AsyncSession, user: User, workspace_id: uuid.UUID
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
    return row[0], row[1]


async def require_member(
    session: AsyncSession, user_id: uuid.UUID, workspace_id: uuid.UUID
) -> None:
    found = await session.scalar(
        select(WorkspaceMember.id).where(
            WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id
        )
    )
    if found is None:
        raise NotFoundError("Not found.")


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
