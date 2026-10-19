"""Mint tokens the way the web app's server does, and sign people in, for tests."""

import time
import uuid
from typing import Any

import jwt

from app.accounts import OAuthProfile, oauth_sign_in
from app.db.models import User
from app.db.session import SessionFactory
from app.security.auth import ALGORITHM, API_AUDIENCE, SERVICE_AUDIENCE, WEB_ISSUER

TEST_JWT_SECRET = "test-internal-jwt-secret-0123456789abcdef"  # noqa: S105


def web_token(
    user_id: uuid.UUID | str,
    *,
    secret: str = TEST_JWT_SECRET,
    ttl: int = 300,
    issued_at: int | None = None,
    auth_time: int | None = None,
    **claims: Any,
) -> str:
    """The proxy's per-request token for a signed-in user."""
    now = issued_at if issued_at is not None else int(time.time())
    payload = {
        "iss": WEB_ISSUER,
        "aud": API_AUDIENCE,
        "sub": str(user_id),
        "auth_time": auth_time if auth_time is not None else now,
        "iat": now,
        "exp": now + ttl,
        **claims,
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def auth_headers(user_id: uuid.UUID | str, **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {web_token(user_id, **kwargs)}"}


def service_headers(*, secret: str = TEST_JWT_SECRET, sub: str = "web") -> dict[str, str]:
    """The web server's token for /auth."""
    now = int(time.time())
    token = jwt.encode(
        {"iss": WEB_ISSUER, "aud": SERVICE_AUDIENCE, "sub": sub, "iat": now, "exp": now + 60},
        secret,
        algorithm=ALGORITHM,
    )
    return {"Authorization": f"Bearer {token}"}


async def github_user(sf: SessionFactory, github_id: int, login: str, **profile: Any) -> User:
    """Sign in with GitHub, as the web server does on the first visit (creates the user and a
    workspace once)."""
    defaults = {
        "name": login.title(),
        "email": f"{login.lower()}@example.com",
        "email_verified": True,
        "avatar_url": f"https://avatars.example.com/{github_id}",
    }
    async with sf() as session:
        return await oauth_sign_in(
            session,
            OAuthProfile(
                provider="github",
                account_id=str(github_id),
                login=login,
                **{**defaults, **profile},
            ),
        )
