"""Mint tokens the way the web app's proxy does, for tests."""

import time
from typing import Any

import jwt

from app.security.auth import ALGORITHM, API_AUDIENCE, WEB_ISSUER

TEST_JWT_SECRET = "test-internal-jwt-secret-0123456789abcdef"  # noqa: S105


def web_token(
    github_id: int,
    login: str,
    *,
    secret: str = TEST_JWT_SECRET,
    ttl: int = 300,
    issued_at: int | None = None,
    **claims: Any,
) -> str:
    now = issued_at if issued_at is not None else int(time.time())
    payload = {
        "iss": WEB_ISSUER,
        "aud": API_AUDIENCE,
        "sub": str(github_id),
        "login": login,
        "name": login.title(),
        "email": f"{login}@example.com",
        "avatar_url": f"https://avatars.example.com/{github_id}",
        "iat": now,
        "exp": now + ttl,
        **claims,
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def auth_headers(github_id: int, login: str, **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {web_token(github_id, login, **kwargs)}"}
