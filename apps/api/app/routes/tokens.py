"""Workspace access tokens for assistants (the MCP server at /mcp, app/mcp_server.py).

Owners make and revoke them. A token acts as the owner who made it, in this workspace only, and
with at most its role: `viewer` reads; `approver` may also start deploys and report incidents.
No token can approve: approvals stay with people in Groundcrew. The token is shown once; only
its SHA-256 is stored.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.accounts import NotFoundError, require_owner
from app.audit import record
from app.db.models import WorkspaceToken
from app.routes.deps import ClientIp, CurrentUser, SessionDep

router = APIRouter(tags=["tokens"])

PREFIX = "gct_"
MAX_DAYS = 365


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenIn(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=100, examples=["Claude Code on my laptop"])]
    role: Literal["viewer", "approver"] = "viewer"
    days: Annotated[int, Field(ge=1, le=MAX_DAYS)] = 90


class TokenOut(BaseModel):
    id: uuid.UUID
    name: str
    role: str
    prefix: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked: bool


class TokenCreated(TokenOut):
    # Shown only now.
    token: str


def _out(t: WorkspaceToken) -> TokenOut:
    return TokenOut(
        id=t.id,
        name=t.name,
        role=t.role,
        prefix=t.prefix,
        created_at=t.created_at,
        expires_at=t.expires_at,
        last_used_at=t.last_used_at,
        revoked=t.revoked_at is not None,
    )


async def _owner(session: SessionDep, user: CurrentUser, workspace_id: uuid.UUID) -> None:
    try:
        await require_owner(session, user, workspace_id)
    except NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found.") from None


@router.get("/workspaces/{workspace_id}/tokens")
async def list_tokens(
    workspace_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> list[TokenOut]:
    await _owner(session, user, workspace_id)
    rows = await session.scalars(
        select(WorkspaceToken)
        .where(WorkspaceToken.workspace_id == workspace_id)
        .order_by(WorkspaceToken.created_at.desc())
    )
    return [_out(t) for t in rows]


@router.post("/workspaces/{workspace_id}/tokens", status_code=status.HTTP_201_CREATED)
async def create_token(
    workspace_id: uuid.UUID, body: TokenIn, user: CurrentUser, session: SessionDep, ip: ClientIp
) -> TokenCreated:
    """A new token, shown once. It acts as you, in this workspace only."""
    await _owner(session, user, workspace_id)
    token = PREFIX + secrets.token_urlsafe(32)
    row = WorkspaceToken(
        workspace_id=workspace_id,
        user_id=user.id,
        name=body.name.strip(),
        role=body.role,
        token_hash=hash_token(token),
        prefix=token[:10],
        expires_at=datetime.now(UTC) + timedelta(days=body.days),
    )
    session.add(row)
    await session.flush()
    record(
        session,
        workspace_id=workspace_id,
        actor=user,
        action="token.created",
        target_type="token",
        target_id=row.id,
        detail={"name": row.name, "role": row.role, "days": body.days},
        ip=ip,
    )
    await session.commit()
    await session.refresh(row)
    return TokenCreated(**_out(row).model_dump(), token=token)


@router.delete("/workspaces/{workspace_id}/tokens/{token_id}")
async def revoke_token(
    workspace_id: uuid.UUID,
    token_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    ip: ClientIp,
) -> TokenOut:
    """Stops the token at once."""
    await _owner(session, user, workspace_id)
    row = await session.get(WorkspaceToken, token_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Token not found.")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        record(
            session,
            workspace_id=workspace_id,
            actor=user,
            action="token.revoked",
            target_type="token",
            target_id=row.id,
            detail={"name": row.name},
            ip=ip,
        )
    await session.commit()
    return _out(row)
