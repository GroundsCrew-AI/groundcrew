"""Shared route dependencies."""

import json
import uuid
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import EXAMPLE_READ_ONLY
from app.config import Settings, get_settings
from app.db.models import Approval, Connection, Incident, Run, User, Workspace
from app.db.session import SessionFactory, get_session, get_session_factory
from app.security.auth import AuthConfigError, AuthError, verify_internal_token
from app.security.ratelimit import limiter

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SessionFactoryDep = Annotated[SessionFactory, Depends(get_session_factory)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )


def too_many_requests(retry_after: int = 60) -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "Too many requests. Wait a minute and try again.",
        headers={"Retry-After": str(retry_after)},
    )


def bearer_token(authorization: Annotated[str | None, Header()] = None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _unauthorized("Sign in required.")
    return token


async def current_user(
    token: Annotated[str, Depends(bearer_token)],
    session: SessionDep,
    settings: SettingsDep,
) -> User:
    """The signed-in user, from the internal JWT the web app attaches to every call. A session
    that started before the password was changed or reset is refused. Each user may make
    RATE_LIMIT_USER_PER_MINUTE calls a minute (429 after that)."""
    try:
        identity = verify_internal_token(token, settings)
    except AuthConfigError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    except AuthError as exc:
        raise _unauthorized(str(exc)) from None
    if not limiter.allow(f"api:user:{identity.user_id}", settings.rate_limit_user_per_minute, 60):
        raise too_many_requests()
    user = await session.get(User, identity.user_id)
    if user is None:
        raise _unauthorized("Sign in again.")
    valid_after = user.sessions_valid_after
    if valid_after is not None and identity.auth_time < int(valid_after.timestamp()):
        raise _unauthorized("Your session ended. Sign in again.")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


# Path and body fields that name what a request changes, and the table that holds its workspace.
_TARGETS: dict[str, type[Any]] = {
    "run_id": Run,
    "deploy_id": Run,
    "approval_id": Approval,
    "connection_id": Connection,
    "incident_id": Incident,
}
_READS = frozenset({"GET", "HEAD", "OPTIONS"})


async def _target_workspaces(request: Request, session: AsyncSession) -> set[uuid.UUID]:
    values: dict[str, Any] = dict(request.path_params)
    if "workspace_id" in request.query_params:
        values.setdefault("workspace_id", request.query_params["workspace_id"])
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            body = None
        if isinstance(body, dict):
            for key in ("workspace_id", *_TARGETS):
                if key in body:
                    values.setdefault(key, body[key])
    out: set[uuid.UUID] = set()
    for key, raw in values.items():
        try:
            value = uuid.UUID(str(raw))
        except ValueError:
            continue
        if key == "workspace_id":
            out.add(value)
        elif (model := _TARGETS.get(key)) is not None:
            found = await session.scalar(select(model.workspace_id).where(model.id == value))
            if found is not None:
                out.add(found)
    return out


async def example_read_only(request: Request, session: SessionDep) -> None:
    """Every route's guard: nothing that writes reaches the example workspace
    (Workspace.is_example), whoever asks. It runs before the route and its body validation, from
    the ids in the path, query, and JSON body; app.accounts refuses more than a viewer there too."""
    if request.method in _READS:
        return
    targets = await _target_workspaces(request, session)
    if targets and await session.scalar(
        select(Workspace.id).where(Workspace.id.in_(targets), Workspace.is_example.is_(True))
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, EXAMPLE_READ_ONLY)


def client_ip(request: Request) -> str | None:
    """The browser's address, as the web server passes it (X-Client-IP), for rate limits and
    the audit log. Not part of the API schema."""
    value = request.headers.get("x-client-ip")
    return value.strip()[:64] if value else None


ClientIp = Annotated[str | None, Depends(client_ip)]
