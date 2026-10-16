"""Workspaces as a team: create one, its name and settings, members and invites.

Reading is for members; changing anything is for owners (403 for members, 404 for everyone
else). A workspace always keeps at least one owner. Members are added by GitHub login: an
existing user joins at once, anyone else gets an invite that applies when they first sign in.
"""

import re
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import func, select

from app.accounts import ForbiddenError, NotFoundError, member_workspace, require_owner
from app.db.models import User, Workspace, WorkspaceInvite, WorkspaceMember, WorkspaceRole
from app.routes.deps import CurrentUser, SessionDep
from app.routes.schemas import WorkspaceOut
from app.workspace_settings import WorkspaceConfig, get_config, set_config

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class WorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name


class WorkspaceDetail(BaseModel):
    id: uuid.UUID
    name: str
    role: WorkspaceRole
    settings: WorkspaceConfig
    created_at: datetime


class WorkspaceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name | None = None
    # Replaces the settings as a whole; send the current values for fields you keep.
    settings: WorkspaceConfig | None = None


class MemberOut(BaseModel):
    user_id: uuid.UUID
    github_login: str | None
    name: str | None
    avatar_url: str | None
    role: WorkspaceRole
    joined_at: datetime


class InviteOut(BaseModel):
    id: uuid.UUID
    github_login: str
    role: WorkspaceRole
    created_at: datetime


class MembersOut(BaseModel):
    members: list[MemberOut]
    # GitHub logins invited who have not signed in yet.
    invites: list[InviteOut]


class MemberAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    github_login: str = Field(min_length=1, max_length=39, examples=["octocat"])
    role: WorkspaceRole = WorkspaceRole.member


class MemberUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: WorkspaceRole


def _not_found(what: str = "Workspace") -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found.")


async def _owner(session: SessionDep, user: User, workspace_id: uuid.UUID) -> Workspace:
    try:
        return await require_owner(session, user, workspace_id)
    except NotFoundError:
        raise _not_found() from None


async def _members(session: SessionDep, workspace_id: uuid.UUID) -> MembersOut:
    rows = await session.execute(
        select(WorkspaceMember, User)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == workspace_id)
        .order_by(WorkspaceMember.created_at)
    )
    invites = await session.scalars(
        select(WorkspaceInvite)
        .where(WorkspaceInvite.workspace_id == workspace_id)
        .order_by(WorkspaceInvite.created_at)
    )
    return MembersOut(
        members=[
            MemberOut(
                user_id=u.id,
                github_login=u.github_login,
                name=u.name,
                avatar_url=u.avatar_url,
                role=m.role,
                joined_at=m.created_at,
            )
            for m, u in rows
        ],
        invites=[
            InviteOut(id=i.id, github_login=i.github_login, role=i.role, created_at=i.created_at)
            for i in invites
        ],
    )


async def _owner_count(session: SessionDep, workspace_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count(WorkspaceMember.id)).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.role == WorkspaceRole.owner,
            )
        )
        or 0
    )


async def _membership(
    session: SessionDep, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> WorkspaceMember:
    member = await session.scalar(
        select(WorkspaceMember)
        .where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id)
        .with_for_update()
    )
    if member is None:
        raise _not_found("Member")
    return member


def _last_owner() -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        "A workspace needs at least one owner. Make someone else an owner first.",
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: WorkspaceCreate, user: CurrentUser, session: SessionDep
) -> WorkspaceOut:
    """A new workspace, owned by you (for example one per client or environment)."""
    workspace = Workspace(name=body.name, owner_id=user.id)
    session.add(workspace)
    await session.flush()
    session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.owner)
    )
    await session.commit()
    return WorkspaceOut.build(workspace, WorkspaceRole.owner)


@router.get("/{workspace_id}")
async def get_workspace(
    workspace_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> WorkspaceDetail:
    try:
        workspace, role = await member_workspace(session, user, workspace_id)
    except NotFoundError:
        raise _not_found() from None
    return WorkspaceDetail(
        id=workspace.id,
        name=workspace.name,
        role=role,
        settings=get_config(workspace),
        created_at=workspace.created_at,
    )


@router.patch("/{workspace_id}")
async def update_workspace(
    workspace_id: uuid.UUID, body: WorkspaceUpdate, user: CurrentUser, session: SessionDep
) -> WorkspaceDetail:
    """Rename the workspace or change its settings (owners)."""
    workspace = await _owner(session, user, workspace_id)
    if body.name is not None:
        workspace.name = body.name
    if body.settings is not None:
        set_config(workspace, body.settings)
    await session.commit()
    return WorkspaceDetail(
        id=workspace.id,
        name=workspace.name,
        role=WorkspaceRole.owner,
        settings=get_config(workspace),
        created_at=workspace.created_at,
    )


@router.get("/{workspace_id}/members")
async def list_members(
    workspace_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> MembersOut:
    try:
        await member_workspace(session, user, workspace_id)
    except NotFoundError:
        raise _not_found() from None
    return await _members(session, workspace_id)


@router.post("/{workspace_id}/members")
async def add_member(
    workspace_id: uuid.UUID, body: MemberAdd, user: CurrentUser, session: SessionDep
) -> MembersOut:
    """Add someone by GitHub login (owners). They join at once if they have signed in to
    Groundcrew before; otherwise the invite applies when they first sign in."""
    await _owner(session, user, workspace_id)
    login = body.github_login.strip().lstrip("@")
    if not _LOGIN.match(login):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That is not a GitHub login.")
    existing = await session.scalar(
        select(User).where(func.lower(User.github_login) == login.lower())
    )
    if existing is not None:
        member = await session.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == existing.id
            )
        )
        if member is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, f"{login} is already a member.")
        session.add(WorkspaceMember(workspace_id=workspace_id, user_id=existing.id, role=body.role))
    else:
        invite = await session.scalar(
            select(WorkspaceInvite).where(
                WorkspaceInvite.workspace_id == workspace_id,
                WorkspaceInvite.github_login == login.lower(),
            )
        )
        if invite is None:
            session.add(
                WorkspaceInvite(
                    workspace_id=workspace_id,
                    github_login=login.lower(),
                    role=body.role,
                    invited_by=user.id,
                )
            )
        else:
            invite.role = body.role
    await session.commit()
    return await _members(session, workspace_id)


@router.patch("/{workspace_id}/members/{user_id}")
async def update_member(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    body: MemberUpdate,
    user: CurrentUser,
    session: SessionDep,
) -> MembersOut:
    """Change a member's role (owners). The last owner cannot be made a member."""
    await _owner(session, user, workspace_id)
    member = await _membership(session, workspace_id, user_id)
    if (
        member.role == WorkspaceRole.owner
        and body.role != WorkspaceRole.owner
        and await _owner_count(session, workspace_id) <= 1
    ):
        raise _last_owner()
    member.role = body.role
    await session.commit()
    return await _members(session, workspace_id)


@router.delete("/{workspace_id}/members/{user_id}")
async def remove_member(
    workspace_id: uuid.UUID, user_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> MembersOut:
    """Remove a member (owners), or leave the workspace yourself. The last owner stays."""
    try:
        _, role = await member_workspace(session, user, workspace_id)
    except NotFoundError:
        raise _not_found() from None
    if user_id != user.id and role != WorkspaceRole.owner:
        raise ForbiddenError("Only a workspace owner can remove other members.")
    member = await _membership(session, workspace_id, user_id)
    if member.role == WorkspaceRole.owner and await _owner_count(session, workspace_id) <= 1:
        raise _last_owner()
    await session.delete(member)
    await session.commit()
    return await _members(session, workspace_id)


@router.delete("/{workspace_id}/invites/{invite_id}")
async def cancel_invite(
    workspace_id: uuid.UUID, invite_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> MembersOut:
    await _owner(session, user, workspace_id)
    invite = await session.get(WorkspaceInvite, invite_id)
    if invite is None or invite.workspace_id != workspace_id:
        raise _not_found("Invite")
    await session.delete(invite)
    await session.commit()
    return await _members(session, workspace_id)
