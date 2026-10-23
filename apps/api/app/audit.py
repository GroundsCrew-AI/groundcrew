"""The audit log: who did what to a workspace, and when (audit_events).

Routes call `record` in the same transaction as the change, so an event exists exactly when
the change was committed. Details hold short, non-secret facts only (a role, a tool name, a
connection kind); never a token, an env value, or a webhook URL.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import display_name
from app.assistant_context import current
from app.db.models import AuditEvent, User


def actor_label(user: User | None) -> str:
    if user is None:
        return "Groundcrew"
    if user.github_login:
        return f"@{user.github_login}"
    return user.email or display_name(user)


def record(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID | None,
    actor: User | None,
    action: str,
    target_type: str | None = None,
    target_id: uuid.UUID | str | None = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> AuditEvent:
    """Add one event to the session (the caller commits with its change). An event an
    assistant caused (through an MCP workspace token) says so, and names the token."""
    label = actor_label(actor)
    assistant = current.get()
    if assistant is not None:
        label = f"{label} via assistant ({assistant.token_name})"
        detail = {**(detail or {}), "assistant_token": str(assistant.token_id)}
    event = AuditEvent(
        workspace_id=workspace_id,
        actor_id=actor.id if actor else None,
        actor_label=label[:320],
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        detail=detail or {},
        ip=ip,
    )
    session.add(event)
    return event
