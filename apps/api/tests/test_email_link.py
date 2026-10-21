"""Sign-in by emailed link: single use, short-lived, the same account for the same verified
address, a new account (with a workspace) on the first sign-in, and nothing that says whether
an address has an account."""

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from urllib.parse import unquote

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, update

from app.config import Settings, get_settings
from app.db.models import AuthToken, User, Workspace, WorkspaceMember
from app.db.session import SessionFactory, get_session_factory
from app.mail import MemoryMailer, get_mailer
from app.main import app
from app.platform import set_signups_paused
from tests.auth_helpers import TEST_JWT_SECRET, github_user, service_headers

pytestmark = pytest.mark.anyio


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "internal_jwt_secret": SecretStr(TEST_JWT_SECRET),
        "web_origin": "https://groundkrew.com",
        **overrides,
    }
    return Settings(_env_file=None, **values)


@pytest.fixture
def mailer() -> MemoryMailer:
    return MemoryMailer()


@pytest.fixture
async def client(
    session_factory: SessionFactory, mailer: MemoryMailer
) -> AsyncIterator[httpx.AsyncClient]:
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = lambda: _settings()
    app.dependency_overrides[get_mailer] = lambda: mailer
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _ask(client: httpx.AsyncClient, email: str, ip: str = "203.0.113.1") -> httpx.Response:
    return await client.post(
        "/auth/email-link",
        json={"email": email},
        headers={**service_headers(), "x-client-ip": ip},
    )


async def _use(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await client.post(
        "/auth/email-link/verify", json={"token": token}, headers=service_headers()
    )


def _token(mailer: MemoryMailer) -> str:
    match = re.search(
        r"https://groundkrew\.com/signin/email\?token=([^\s&]+)", mailer.outbox[-1].text
    )
    assert match, mailer.outbox[-1].text
    return unquote(match.group(1))


async def test_first_link_creates_the_account_and_a_workspace(
    client: httpx.AsyncClient, session_factory: SessionFactory, mailer: MemoryMailer
) -> None:
    r = await _ask(client, " Ada@Example.com ")
    assert r.status_code == 202 and r.json() == {"status": "check_email"}
    assert mailer.outbox[-1].to == "ada@example.com"
    assert "expires in 15 minutes" in mailer.outbox[-1].text
    token = _token(mailer)
    async with session_factory() as session:
        assert await session.scalar(select(User).where(User.email == "ada@example.com")) is None
        row = await session.scalar(select(AuthToken))
        assert row is not None and row.user_id is None and token not in row.token_hash

    r = await _use(client, token)
    assert r.status_code == 200, r.text
    assert r.json()["email"] == "ada@example.com" and r.json()["email_verified"] is True
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.email == "ada@example.com"))
        assert user is not None and user.password_hash is None
        owned = await session.scalar(
            select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)
        )
        assert owned is not None and owned.role == "owner"

    # Single use.
    again = await _use(client, token)
    assert again.status_code == 400 and again.json()["code"] == "invalid_link"


async def test_the_same_verified_address_is_the_same_user(
    client: httpx.AsyncClient, session_factory: SessionFactory, mailer: MemoryMailer
) -> None:
    gh = await github_user(session_factory, 4242, "ada", email="ada@example.com")
    await _ask(client, "ada@example.com")
    r = await _use(client, _token(mailer))
    assert r.json()["id"] == str(gh.id)
    async with session_factory() as session:
        count = await session.scalar(select(Workspace.id).where(Workspace.owner_id == gh.id))
        assert count is not None  # still the one workspace from GitHub sign-in


async def test_links_expire_and_a_newer_one_retires_the_rest(
    client: httpx.AsyncClient, session_factory: SessionFactory, mailer: MemoryMailer
) -> None:
    await _ask(client, "bo@example.com")
    first = _token(mailer)
    await _ask(client, "bo@example.com")
    second = _token(mailer)
    assert (await _use(client, second)).status_code == 200
    assert (await _use(client, first)).status_code == 400

    await _ask(client, "cy@example.com")
    stale = _token(mailer)
    async with session_factory() as session:
        await session.execute(
            update(AuthToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    assert (await _use(client, stale)).status_code == 400


async def test_unproven_password_account_is_taken_over_by_the_link(
    client: httpx.AsyncClient, session_factory: SessionFactory, mailer: MemoryMailer
) -> None:
    async with session_factory() as session:
        squatter = User(email="dee@example.com", password_hash="x")  # noqa: S106
        session.add(squatter)
        await session.commit()
    await _ask(client, "dee@example.com")
    r = await _use(client, _token(mailer))
    assert r.json()["id"] == str(squatter.id)
    async with session_factory() as session:
        user = await session.get_one(User, squatter.id)
        assert user.password_hash is None and user.email_verified_at is not None
        assert user.sessions_valid_after is not None


async def test_paused_sign_ups_send_no_link_to_strangers(
    client: httpx.AsyncClient, session_factory: SessionFactory, mailer: MemoryMailer
) -> None:
    await github_user(session_factory, 77, "kim", email="kim@example.com")
    async with session_factory() as session:
        await set_signups_paused(session, True, None)
        await session.commit()
    stranger = await _ask(client, "new@example.com")
    assert stranger.status_code == 202 and mailer.outbox == []
    await _ask(client, "kim@example.com")
    assert len(mailer.outbox) == 1


async def test_rate_limits_and_switch(client: httpx.AsyncClient, mailer: MemoryMailer) -> None:
    for _ in range(5):
        assert (await _ask(client, "eve@example.com")).status_code == 202
    r = await _ask(client, "eve@example.com")
    assert r.status_code == 429 and r.json()["code"] == "rate_limited"
    assert len(mailer.outbox) == 5

    app.dependency_overrides[get_settings] = lambda: _settings(email_link_auth_enabled=False)
    r = await _ask(client, "fay@example.com")
    assert r.status_code == 403 and r.json()["code"] == "disabled"
    assert (await client.get("/auth/options", headers=service_headers())).json()[
        "email_link"
    ] is False


def test_production_needs_smtp_for_email_links() -> None:
    prod = _settings(
        app_env="production",
        database_url=SecretStr("postgresql+asyncpg://u:p@db/gc"),
        secrets_key=SecretStr("x" * 40),
        approval_token_key=SecretStr("x" * 40),
        k8s_allow_private_servers=None,
    )
    assert any("email sign-in links" in p for p in prod.production_problems())
    assert prod.model_copy(update={"smtp_host": "smtp.resend.com"}).production_problems() == []


async def test_a_mail_failure_is_a_clear_503(client: httpx.AsyncClient) -> None:
    from app.mail import MailError

    with patch.object(MemoryMailer, "send", side_effect=MailError("down")):
        r = await _ask(client, "gus@example.com")
    assert r.status_code == 503 and r.json()["code"] == "email_failed"
