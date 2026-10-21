"""Sign-in endpoints for the web server: provider accounts, email and password, email links.

Only the web server calls these, with a service token (app.security.auth). A user's own token
is refused here, so nobody can claim a provider account from the browser. The web server
passes the browser's address in X-Client-IP for rate limits.

Nothing here says whether an address has an account: sign-up, "resend the link", and "forgot
password" answer the same either way and tell the address's owner by email instead.
Failures are {"detail": "...", "code": "..."} so the web app can show the right next step.
"""

import logging
import re
import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from app import accounts
from app.accounts import LinkError, OAuthProfile, SignInError, normalize_email
from app.db.models import AuthTokenPurpose, User
from app.mail import (
    Mail,
    Mailer,
    MailError,
    account_exists_mail,
    get_mailer,
    reset_mail,
    verification_mail,
)
from app.platform import SignupRefusedError
from app.routes.deps import SessionDep, SettingsDep, bearer_token
from app.security.auth import AuthConfigError, AuthError, verify_service_token
from app.security.passwords import MAX_LENGTH, hash_password_async, password_problem
from app.security.ratelimit import limiter

log = logging.getLogger(__name__)

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
# How many links one account may be sent per hour (each kind).
LINKS_PER_HOUR = 5


class AuthFlowError(Exception):
    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code, self.code, self.detail = status_code, code, detail


def service_caller(token: Annotated[str, Depends(bearer_token)], settings: SettingsDep) -> None:
    try:
        verify_service_token(token, settings)
    except AuthConfigError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    except AuthError:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Service token required.", {"WWW-Authenticate": "Bearer"}
        ) from None


router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(service_caller)])
MailerDep = Annotated[Mailer, Depends(get_mailer)]


def _client(request: Request, x_client_ip: str | None) -> str:
    if x_client_ip:
        return x_client_ip.strip()[:64]
    return request.client.host if request.client else "unknown"


ClientIp = Annotated[str | None, Header()]


def _limit(key: str, limit: int, window_seconds: float) -> None:
    if not limiter.allow(key, limit, window_seconds):
        raise AuthFlowError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate_limited",
            "Too many attempts. Wait a few minutes and try again.",
        )


def _require_password_auth(enabled: bool) -> None:
    if not enabled:
        raise AuthFlowError(
            status.HTTP_403_FORBIDDEN, "disabled", "Email sign-in is turned off on this server."
        )


class Email(BaseModel):
    email: str = Field(max_length=320, examples=["ada@example.com"])

    @field_validator("email")
    @classmethod
    def _valid(cls, value: str) -> str:
        value = normalize_email(value)
        if not _EMAIL.match(value):
            raise ValueError("Enter a valid email address.")
        return value


class SignIn(Email):
    password: str = Field(min_length=1, max_length=MAX_LENGTH)


class SignUp(Email):
    password: str = Field(min_length=1, max_length=MAX_LENGTH)
    name: str | None = Field(default=None, max_length=200)


class LinkWithPassword(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=1, max_length=MAX_LENGTH)


class AuthUser(BaseModel):
    """Who signed in. The web server keeps `id` in its session and sends it with each call."""

    id: uuid.UUID
    email: str | None
    email_verified: bool
    name: str | None
    image: str | None

    @classmethod
    def of(cls, user: User) -> "AuthUser":
        return cls(
            id=user.id,
            email=user.email,
            email_verified=user.email_verified_at is not None,
            name=user.name or user.github_login,
            image=user.avatar_url,
        )


class Accepted(BaseModel):
    status: str = "check_email"


class Options(BaseModel):
    password: bool
    google: bool
    require_verified_email: bool
    password_min_length: int


async def _send(mailer: Mailer, mail_: Mail) -> None:
    try:
        await mailer.send(mail_)
    except MailError:
        raise AuthFlowError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "email_failed",
            "We could not send the email. Try again in a few minutes.",
        ) from None


def _check_password(password: str, min_length: int, email: str | None = None) -> None:
    problem = password_problem(password, min_length=min_length, email=email)
    if problem:
        raise AuthFlowError(status.HTTP_422_UNPROCESSABLE_CONTENT, "weak_password", problem)


@router.get("/options")
async def options(settings: SettingsDep) -> Options:
    """What the sign-in page offers."""
    return Options(
        password=settings.password_auth,
        google=settings.google_auth,
        require_verified_email=settings.auth_require_verified_email,
        password_min_length=settings.password_min_length,
    )


def _signup_refused(exc: SignupRefusedError) -> AuthFlowError:
    paused = exc.code == "signups_paused"
    return AuthFlowError(
        status.HTTP_403_FORBIDDEN if paused else status.HTTP_429_TOO_MANY_REQUESTS,
        exc.code,
        str(exc),
    )


@router.post("/oauth")
async def oauth(
    body: OAuthProfile,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    x_client_ip: ClientIp = None,
) -> AuthUser:
    """A GitHub or Google sign-in: the user, linked or created. Google only where it is on
    (GOOGLE_AUTH_ENABLED; off on hosted instances by default)."""
    if body.provider == "google" and not settings.google_auth:
        raise AuthFlowError(
            status.HTTP_403_FORBIDDEN, "disabled", "Google sign-in is turned off on this server."
        )
    try:
        user = await accounts.oauth_sign_in(
            session, body, settings=settings, ip=_client(request, x_client_ip)
        )
    except SignupRefusedError as exc:
        raise _signup_refused(exc) from None
    return AuthUser.of(user)


@router.post("/sign-in")
async def sign_in(
    body: SignIn,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    x_client_ip: ClientIp = None,
) -> AuthUser:
    _require_password_auth(settings.password_auth)
    _limit(f"sign-in:ip:{_client(request, x_client_ip)}", 30, 600)
    _limit(f"sign-in:email:{body.email}", 10, 900)
    try:
        user = await accounts.password_sign_in(session, body.email, body.password, settings)
    except SignInError as exc:
        if exc.code == "unverified":
            raise AuthFlowError(
                status.HTTP_403_FORBIDDEN,
                "unverified",
                "Confirm your email address first: open the link we sent you.",
            ) from None
        if exc.code == "locked":
            raise AuthFlowError(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "locked",
                f"Too many wrong passwords. Try again in {settings.auth_lockout_minutes} minutes,"
                " or reset your password.",
            ) from None
        raise AuthFlowError(
            status.HTTP_401_UNAUTHORIZED, "invalid", "Email or password is incorrect."
        ) from None
    return AuthUser.of(user)


@router.post("/sign-up", status_code=status.HTTP_202_ACCEPTED)
async def sign_up(
    body: SignUp,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    mailer: MailerDep,
    x_client_ip: ClientIp = None,
) -> Accepted:
    """Create a password account and send the confirmation link. For an address that already
    has an account, its owner gets an email saying so (or a new link, if they never
    confirmed); the answer here is the same."""
    _require_password_auth(settings.password_auth)
    _check_password(body.password, settings.password_min_length, body.email)
    _limit(f"sign-up:ip:{_client(request, x_client_ip)}", 10, 3600)
    name = (body.name or "").strip() or None
    try:
        user, created = await accounts.password_sign_up(
            session,
            body.email,
            await hash_password_async(body.password),
            name,
            settings=settings,
            ip=_client(request, x_client_ip),
        )
    except SignupRefusedError as exc:
        raise _signup_refused(exc) from None
    if created or (user.email_verified_at is None and user.password_hash is not None):
        await _send_verification(session, user, settings, mailer)
    else:
        await _send(mailer, account_exists_mail(settings, body.email))
    return Accepted()


async def _send_verification(
    session: SessionDep, user: User, settings: SettingsDep, mailer: Mailer
) -> None:
    purpose = AuthTokenPurpose.verify_email
    if await accounts.links_sent_recently(session, user, purpose, timedelta(hours=1)) >= (
        LINKS_PER_HOUR
    ):
        return
    ttl = timedelta(hours=settings.email_verification_hours)
    token = await accounts.issue_link(session, user, purpose, ttl)
    await session.commit()
    await _send(mailer, verification_mail(settings, user.email or "", token))


@router.post("/resend-verification", status_code=status.HTTP_202_ACCEPTED)
async def resend_verification(
    body: Email,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    mailer: MailerDep,
    x_client_ip: ClientIp = None,
) -> Accepted:
    _require_password_auth(settings.password_auth)
    _limit(f"links:ip:{_client(request, x_client_ip)}", 20, 3600)
    _limit(f"verify:email:{body.email}", 5, 3600)
    user = await accounts.user_by_email(session, body.email)
    if user is not None and user.email_verified_at is None and user.password_hash is not None:
        await _send_verification(session, user, settings, mailer)
    return Accepted()


@router.post("/verify-email")
async def verify_email(
    body: LinkWithPassword,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    x_client_ip: ClientIp = None,
) -> AuthUser:
    """Confirm the address from the link; the account's password proves it is theirs."""
    _require_password_auth(settings.password_auth)
    _limit(f"link:ip:{_client(request, x_client_ip)}", 30, 3600)
    try:
        user = await accounts.verify_email(session, body.token, body.password)
    except LinkError as exc:
        raise AuthFlowError(status.HTTP_400_BAD_REQUEST, "invalid_link", str(exc)) from None
    except SignInError:
        raise AuthFlowError(
            status.HTTP_401_UNAUTHORIZED,
            "invalid",
            "That is not the password of the account this link is for.",
        ) from None
    return AuthUser.of(user)


@router.post("/forgot-password", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(
    body: Email,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    mailer: MailerDep,
    x_client_ip: ClientIp = None,
) -> Accepted:
    """Send a reset link if the address has an account (GitHub and Google accounts too: the
    link adds a password)."""
    _require_password_auth(settings.password_auth)
    _limit(f"links:ip:{_client(request, x_client_ip)}", 20, 3600)
    _limit(f"reset:email:{body.email}", 5, 3600)
    user = await accounts.user_by_email(session, body.email)
    if user is None:
        return Accepted()
    purpose = AuthTokenPurpose.reset_password
    if await accounts.links_sent_recently(session, user, purpose, timedelta(hours=1)) >= (
        LINKS_PER_HOUR
    ):
        return Accepted()
    ttl = timedelta(minutes=settings.password_reset_minutes)
    token = await accounts.issue_link(session, user, purpose, ttl)
    await session.commit()
    await _send(mailer, reset_mail(settings, user.email or body.email, token))
    return Accepted()


@router.post("/reset-password")
async def reset_password(
    body: LinkWithPassword,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    x_client_ip: ClientIp = None,
) -> AuthUser:
    """Choose a new password from a reset link. Every existing session ends."""
    _require_password_auth(settings.password_auth)
    _check_password(body.password, settings.password_min_length)
    _limit(f"link:ip:{_client(request, x_client_ip)}", 30, 3600)
    try:
        user = await accounts.reset_password(
            session, body.token, await hash_password_async(body.password)
        )
    except LinkError as exc:
        raise AuthFlowError(status.HTTP_400_BAD_REQUEST, "invalid_link", str(exc)) from None
    return AuthUser.of(user)
