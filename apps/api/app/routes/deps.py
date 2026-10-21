"""Shared route dependencies."""

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import User
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


def client_ip(request: Request) -> str | None:
    """The browser's address, as the web server passes it (X-Client-IP), for rate limits and
    the audit log. Not part of the API schema."""
    value = request.headers.get("x-client-ip")
    return value.strip()[:64] if value else None


ClientIp = Annotated[str | None, Depends(client_ip)]
