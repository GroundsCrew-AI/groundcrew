"""Workspaces as a team: create one, its name and settings, members and invites.

Reading is for members; changing anything is for owners (403 for approvers and viewers, 404
for everyone else). A workspace always keeps at least one owner. Every change is written to the
audit log (app.audit), which every member can read. Members are added by email address or
GitHub login: an existing user (with that confirmed address, or that GitHub account) joins at
once; anyone else gets an invite that applies when they sign up or sign in. Email invites are
also sent by email.

Notifications (app.notifications) are part of the settings; the Slack webhook is write-only:
saved encrypted, and only `slack_saved` comes back.
"""

import logging
import re
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import ColumnElement, func, select

from app.accounts import (
    ForbiddenError,
    NotFoundError,
    display_name,
    member_workspace,
    normalize_email,
    require_owner,
)
from app.audit import record
from app.db.models import (
    AuditEvent,
    User,
    Workspace,
    WorkspaceInvite,
    WorkspaceMember,
    WorkspaceRole,
)
from app.mail import Mailer, MailError, get_mailer, invite_mail
from app.notifications import send_test
from app.routes.deps import ClientIp, CurrentUser, SessionDep, SettingsDep
from app.routes.schemas import WorkspaceOut
from app.security.crypto import SecretsKeyError
from app.workspace_settings import (
    SLACK_WEBHOOK,
    WorkspaceConfig,
    get_config,
    set_config,
    set_slack_webhook,
    slack_saved,
)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])
log = logging.getLogger(__name__)

_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class WorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name


class WorkspaceDetail(BaseModel):
    id: uuid.UUID
    name: str
    role: WorkspaceRole
    settings: WorkspaceConfig
    # A Slack webhook is saved for notifications. The URL itself is never returned.
    slack_saved: bool = False
    created_at: datetime


class WorkspaceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name | None = None
    # Replaces the settings as a whole; send the current values for fields you keep.
    # `notifications` left out keeps the saved ones.
    settings: WorkspaceConfig | None = None
    # A Slack incoming webhook (https://hooks.slack.com/services/...) saves it, encrypted;
    # null removes it; left out keeps it.
    slack_webhook_url: str | None = Field(default=None, max_length=500)


class ChannelTestOut(BaseModel):
    channel: Literal["slack", "email"]
    ok: bool
    # What happened: "Sent to 2 people.", or the error.
    detail: str


class NotificationTestOut(BaseModel):
    # One per channel that is set up; empty when none is.
    results: list[ChannelTestOut]


class MemberOut(BaseModel):
    user_id: uuid.UUID
    github_login: str | None
    name: str | None
    email: str | None = None
    avatar_url: str | None
    role: WorkspaceRole
    joined_at: datetime


class InviteOut(BaseModel):
    id: uuid.UUID
    # One of the two is set.
    github_login: str | None
    email: str | None = None
    role: WorkspaceRole
    created_at: datetime


class MembersOut(BaseModel):
    members: list[MemberOut]
    # People invited who have not joined yet.
    invites: list[InviteOut]


class MemberAdd(BaseModel):
    """Add by `email` or by `github_login` (exactly one)."""

    model_config = ConfigDict(extra="forbid")
    email: str | None = Field(default=None, max_length=320, examples=["ada@example.com"])
    github_login: str | None = Field(default=None, min_length=1, max_length=40)
    # Viewers read; approvers also approve deploys and fixes; owners manage the workspace.
    role: WorkspaceRole = WorkspaceRole.viewer

    @model_validator(mode="after")
    def _one_target(self) -> "MemberAdd":
        if (self.email is None) == (self.github_login is None):
            raise ValueError("Send either an email address or a GitHub login.")
        return self


class MemberUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: WorkspaceRole


class AuditEventOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    # "@login" or the email of who did it; "Groundcrew" for the platform itself.
    actor: str
    actor_id: uuid.UUID | None
    # approval.approved, approval.rejected, member.added, member.role_changed, member.removed,
    # invite.created, invite.canceled, workspace.created, workspace.updated, connection.*, ...
    action: str
    target_type: str | None
    target_id: str | None
    detail: dict[str, object]


class AuditPage(BaseModel):
    items: list[AuditEventOut]
    # Pass as `before` for older events; null on the last page.
    next_before: datetime | None


def _detail(workspace: Workspace, role: WorkspaceRole) -> WorkspaceDetail:
    return WorkspaceDetail(
        id=workspace.id,
        name=workspace.name,
        role=role,
        settings=get_config(workspace),
        slack_saved=slack_saved(workspace),
        created_at=workspace.created_at,
    )


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
                email=u.email,
                avatar_url=u.avatar_url,
                role=m.role,
                joined_at=m.created_at,
            )
            for m, u in rows
        ],
        invites=[
            InviteOut(
                id=i.id,
                github_login=i.github_login,
                email=i.email,
                role=i.role,
                created_at=i.created_at,
            )
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
    body: WorkspaceCreate, user: CurrentUser, session: SessionDep, ip: ClientIp
) -> WorkspaceOut:
    """A new workspace, owned by you (for example one per client or environment)."""
    workspace = Workspace(name=body.name, owner_id=user.id)
    session.add(workspace)
    await session.flush()
    session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.owner)
    )
    record(session, workspace_id=workspace.id, actor=user, action="workspace.created", ip=ip)
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
    return _detail(workspace, role)


@router.patch("/{workspace_id}")
async def update_workspace(
    workspace_id: uuid.UUID,
    body: WorkspaceUpdate,
    user: CurrentUser,
    session: SessionDep,
    ip: ClientIp,
) -> WorkspaceDetail:
    """Rename the workspace, change its settings, or save or remove its Slack webhook
    (owners)."""
    workspace = await _owner(session, user, workspace_id)
    if body.name is not None:
        workspace.name = body.name
    if body.settings is not None:
        config = body.settings
        if "notifications" not in config.model_fields_set:
            # Clients that predate notifications send the other settings only.
            config = config.model_copy(
                update={"notifications": get_config(workspace).notifications}
            )
        set_config(workspace, config)
    if "slack_webhook_url" in body.model_fields_set:
        url = (body.slack_webhook_url or "").strip()
        if url and not SLACK_WEBHOOK.match(url):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "That is not a Slack incoming webhook (https://hooks.slack.com/services/...).",
            )
        try:
            set_slack_webhook(workspace, url or None)
        except SecretsKeyError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    changed = sorted(body.model_fields_set & {"name", "settings"})
    if "slack_webhook_url" in body.model_fields_set:
        changed.append("slack_webhook_saved" if body.slack_webhook_url else "slack_webhook_removed")
    record(
        session,
        workspace_id=workspace_id,
        actor=user,
        action="workspace.updated",
        target_type="workspace",
        target_id=workspace_id,
        detail={"changed": changed},
        ip=ip,
    )
    await session.commit()
    return _detail(workspace, WorkspaceRole.owner)


@router.post("/{workspace_id}/notifications/test")
async def test_notifications(
    workspace_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    mailer: Annotated[Mailer, Depends(get_mailer)],
) -> NotificationTestOut:
    """Send a test message to the Slack webhook and the email audience now (owners). One
    result per channel that is set up."""
    workspace = await _owner(session, user, workspace_id)
    results = await send_test(session, settings, workspace, mailer)
    return NotificationTestOut(
        results=[ChannelTestOut(channel=r.channel, ok=r.ok, detail=r.detail) for r in results]
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


async def _add_or_invite(
    session: SessionDep,
    workspace_id: uuid.UUID,
    existing: User | None,
    invite_filter: ColumnElement[bool],
    new_invite: WorkspaceInvite,
    label: str,
) -> bool:
    """Add `existing` at once, or create (or update) the invite. True when a new invite was
    created."""
    if existing is not None:
        member = await session.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == existing.id
            )
        )
        if member is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, f"{label} is already a member.")
        session.add(
            WorkspaceMember(workspace_id=workspace_id, user_id=existing.id, role=new_invite.role)
        )
        return False
    invite = await session.scalar(
        select(WorkspaceInvite).where(WorkspaceInvite.workspace_id == workspace_id, invite_filter)
    )
    if invite is not None:
        invite.role = new_invite.role
        return False
    session.add(new_invite)
    return True


def _record_add(
    session: SessionDep,
    user: User,
    workspace_id: uuid.UUID,
    existing: User | None,
    invited: bool,
    who: dict[str, str],
    role: WorkspaceRole,
    ip: str | None,
) -> None:
    if existing is not None:
        record(
            session,
            workspace_id=workspace_id,
            actor=user,
            action="member.added",
            target_type="user",
            target_id=existing.id,
            detail={**who, "role": str(role)},
            ip=ip,
        )
    else:
        # A new invite, or an existing one whose role changed.
        record(
            session,
            workspace_id=workspace_id,
            actor=user,
            action="invite.created" if invited else "invite.updated",
            target_type="invite",
            detail={**who, "role": str(role)},
            ip=ip,
        )


@router.post("/{workspace_id}/members")
async def add_member(
    workspace_id: uuid.UUID,
    body: MemberAdd,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    mailer: Annotated[Mailer, Depends(get_mailer)],
    ip: ClientIp,
) -> MembersOut:
    """Add someone by email address or GitHub login (owners). They join at once if they have
    an account (with that confirmed address, or that GitHub account); otherwise they are
    invited, by email for an address, and join when they sign up or sign in."""
    workspace = await _owner(session, user, workspace_id)
    if body.email is not None:
        email = normalize_email(body.email)
        if not _EMAIL.match(email):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "That is not an email address."
            )
        existing = await session.scalar(
            select(User).where(func.lower(User.email) == email, User.email_verified_at.is_not(None))
        )
        invited = await _add_or_invite(
            session,
            workspace_id,
            existing,
            WorkspaceInvite.email == email,
            WorkspaceInvite(
                workspace_id=workspace_id, email=email, role=body.role, invited_by=user.id
            ),
            email,
        )
        _record_add(session, user, workspace_id, existing, invited, {"email": email}, body.role, ip)
        await session.commit()
        if invited:
            try:
                await mailer.send(invite_mail(settings, email, workspace.name, display_name(user)))
            except MailError:
                log.warning("invite email to workspace %s was not sent", workspace_id)
        return await _members(session, workspace_id)

    login = (body.github_login or "").strip().lstrip("@")
    if not _LOGIN.match(login):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That is not a GitHub login.")
    existing = await session.scalar(
        select(User).where(func.lower(User.github_login) == login.lower())
    )
    invited = await _add_or_invite(
        session,
        workspace_id,
        existing,
        WorkspaceInvite.github_login == login.lower(),
        WorkspaceInvite(
            workspace_id=workspace_id,
            github_login=login.lower(),
            role=body.role,
            invited_by=user.id,
        ),
        login,
    )
    _record_add(
        session,
        user,
        workspace_id,
        existing,
        invited,
        {"github_login": login},
        body.role,
        ip,
    )
    await session.commit()
    return await _members(session, workspace_id)


@router.patch("/{workspace_id}/members/{user_id}")
async def update_member(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    body: MemberUpdate,
    user: CurrentUser,
    session: SessionDep,
    ip: ClientIp,
) -> MembersOut:
    """Change a member's role (owners). The last owner stays an owner."""
    await _owner(session, user, workspace_id)
    member = await _membership(session, workspace_id, user_id)
    if (
        member.role == WorkspaceRole.owner
        and body.role != WorkspaceRole.owner
        and await _owner_count(session, workspace_id) <= 1
    ):
        raise _last_owner()
    if member.role != body.role:
        record(
            session,
            workspace_id=workspace_id,
            actor=user,
            action="member.role_changed",
            target_type="user",
            target_id=user_id,
            detail={"from": str(member.role), "to": str(body.role)},
            ip=ip,
        )
    member.role = body.role
    await session.commit()
    return await _members(session, workspace_id)


@router.delete("/{workspace_id}/members/{user_id}")
async def remove_member(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    ip: ClientIp,
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
    record(
        session,
        workspace_id=workspace_id,
        actor=user,
        action="member.left" if user_id == user.id else "member.removed",
        target_type="user",
        target_id=user_id,
        detail={"role": str(member.role)},
        ip=ip,
    )
    await session.commit()
    return await _members(session, workspace_id)


@router.delete("/{workspace_id}/invites/{invite_id}")
async def cancel_invite(
    workspace_id: uuid.UUID,
    invite_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    ip: ClientIp,
) -> MembersOut:
    await _owner(session, user, workspace_id)
    invite = await session.get(WorkspaceInvite, invite_id)
    if invite is None or invite.workspace_id != workspace_id:
        raise _not_found("Invite")
    await session.delete(invite)
    who = {"email": invite.email} if invite.email else {"github_login": invite.github_login or ""}
    record(
        session,
        workspace_id=workspace_id,
        actor=user,
        action="invite.canceled",
        target_type="invite",
        target_id=invite_id,
        detail=who,
        ip=ip,
    )
    await session.commit()
    return await _members(session, workspace_id)


@router.get("/{workspace_id}/audit")
async def audit_log(
    workspace_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AuditPage:
    """Who did what in this workspace, newest first (every member)."""
    try:
        await member_workspace(session, user, workspace_id)
    except NotFoundError:
        raise _not_found() from None
    query = select(AuditEvent).where(AuditEvent.workspace_id == workspace_id)
    if before is not None:
        query = query.where(AuditEvent.created_at < before)
    rows = list(
        await session.scalars(query.order_by(AuditEvent.created_at.desc()).limit(limit + 1))
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return AuditPage(
        items=[
            AuditEventOut(
                id=e.id,
                created_at=e.created_at,
                actor=e.actor_label,
                actor_id=e.actor_id,
                action=e.action,
                target_type=e.target_type,
                target_id=e.target_id,
                detail=dict(e.detail or {}),
            )
            for e in rows
        ],
        next_before=rows[-1].created_at if more and rows else None,
    )
