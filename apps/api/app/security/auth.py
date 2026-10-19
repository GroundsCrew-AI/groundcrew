"""Internal JWTs from the web app, service tokens for sign-in, and SSE stream tokens.

The web app (Next.js) signs an HS256 JWT with INTERNAL_JWT_SECRET for every API call it
proxies. It lives 5 minutes and carries the signed-in user's id and when they signed in
(`auth_time`, checked against users.sessions_valid_after). The API never sees the user's
provider tokens or the Auth.js session.

Sign-in itself (/auth/*: OAuth accounts, passwords, email links) is called by the web server
only, with a service token: same secret, its own audience, 60 seconds. A user's token is
never accepted there, and a service token is never accepted anywhere else.

SSE goes from the browser straight to the API, so it cannot carry that JWT. Instead the API
issues a 60-second stream token bound to one run and one user (POST /runs/{id}/stream-token).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings

ALGORITHM = "HS256"
WEB_ISSUER = "groundcrew-web"
API_AUDIENCE = "groundcrew-api"
SERVICE_AUDIENCE = "groundcrew-api-auth"
STREAM_ISSUER = "groundcrew-api"
STREAM_AUDIENCE = "groundcrew-sse"
STREAM_TOKEN_TTL = timedelta(seconds=60)
# The web app signs 5-minute tokens; refuse anything that claims to live much longer.
MAX_INTERNAL_TOKEN_LIFETIME = timedelta(minutes=10)
MIN_SECRET_LENGTH = 32
LEEWAY_SECONDS = 10


class AuthError(Exception):
    """The token is missing, malformed, expired, or for something else. Maps to 401."""


class AuthConfigError(AuthError):
    """INTERNAL_JWT_SECRET is not set or too short. Maps to 503."""


class WebIdentity(BaseModel):
    """Claims the web app puts in the internal JWT."""

    user_id: uuid.UUID
    # When this web session signed in (seconds since the epoch).
    auth_time: int = Field(ge=0)


def _secret(settings: Settings) -> str:
    secret = settings.internal_jwt_secret
    if secret is None or len(secret.get_secret_value()) < MIN_SECRET_LENGTH:
        raise AuthConfigError(
            f"INTERNAL_JWT_SECRET must be set to at least {MIN_SECRET_LENGTH} characters."
        )
    return secret.get_secret_value()


def _decode(token: str, settings: Settings, *, audience: str, issuer: str) -> dict[str, Any]:
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            _secret(settings),
            algorithms=[ALGORITHM],
            audience=audience,
            issuer=issuer,
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError:
        raise AuthError("Token expired.") from None
    except jwt.PyJWTError:
        # Never echo the token or the library's detail back to the caller.
        raise AuthError("Invalid token.") from None
    if claims["exp"] - claims["iat"] > MAX_INTERNAL_TOKEN_LIFETIME.total_seconds():
        raise AuthError("Token lifetime too long.")
    return claims


def verify_internal_token(token: str, settings: Settings) -> WebIdentity:
    claims = _decode(token, settings, audience=API_AUDIENCE, issuer=WEB_ISSUER)
    try:
        return WebIdentity(user_id=uuid.UUID(claims["sub"]), auth_time=claims.get("auth_time"))
    except (ValueError, TypeError, ValidationError):
        raise AuthError("Invalid token claims.") from None


def verify_service_token(token: str, settings: Settings) -> None:
    """A token the web server signed for the sign-in endpoints. Raises AuthError otherwise."""
    claims = _decode(token, settings, audience=SERVICE_AUDIENCE, issuer=WEB_ISSUER)
    if claims.get("sub") != "web":
        raise AuthError("Invalid token claims.")


def create_stream_token(
    user_id: uuid.UUID, run_id: uuid.UUID, settings: Settings, *, now: datetime | None = None
) -> tuple[str, datetime]:
    issued = now or datetime.now(UTC)
    expires = issued + STREAM_TOKEN_TTL
    token = jwt.encode(
        {
            "iss": STREAM_ISSUER,
            "aud": STREAM_AUDIENCE,
            "sub": str(user_id),
            "run_id": str(run_id),
            "iat": int(issued.timestamp()),
            "exp": int(expires.timestamp()),
        },
        _secret(settings),
        algorithm=ALGORITHM,
    )
    return token, expires


def verify_stream_token(token: str, run_id: uuid.UUID, settings: Settings) -> uuid.UUID:
    """The user id the token was issued to. Raises AuthError unless it is for this run."""
    claims = _decode(token, settings, audience=STREAM_AUDIENCE, issuer=STREAM_ISSUER)
    if claims.get("run_id") != str(run_id):
        raise AuthError("Token is for a different run.")
    try:
        return uuid.UUID(claims["sub"])
    except ValueError:
        raise AuthError("Invalid token claims.") from None
