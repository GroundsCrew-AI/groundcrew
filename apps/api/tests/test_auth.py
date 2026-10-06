"""Internal JWT auth, first-login workspaces, and SSE stream tokens."""

import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from app.accounts import upsert_user
from app.config import Settings, get_settings
from app.db.models import Run, RunStatus, User, Workspace, WorkspaceMember, WorkspaceRole
from app.db.session import SessionFactory, get_session_factory
from app.main import app
from app.security.auth import (
    AuthConfigError,
    AuthError,
    WebIdentity,
    create_stream_token,
    verify_internal_token,
    verify_stream_token,
)
from tests.auth_helpers import TEST_JWT_SECRET, auth_headers, web_token

pytestmark = pytest.mark.anyio


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"internal_jwt_secret": SecretStr(TEST_JWT_SECRET), **overrides}
    return Settings(_env_file=None, **values)


@pytest.fixture
async def client(session_factory: SessionFactory) -> AsyncIterator[httpx.AsyncClient]:
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = lambda: _settings()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# --- internal JWT -------------------------------------------------------------------------


def test_valid_internal_token() -> None:
    identity = verify_internal_token(web_token(42, "octo"), _settings())
    assert identity == WebIdentity(
        github_id=42,
        login="octo",
        name="Octo",
        email="octo@example.com",
        avatar_url="https://avatars.example.com/42",
    )


@pytest.mark.parametrize(
    ("token", "message"),
    [
        (web_token(42, "octo", secret="another-secret-that-is-long-enough!!"), "Invalid token"),  # noqa: S106
        (web_token(42, "octo", issued_at=int(time.time()) - 3600), "expired"),
        (web_token(42, "octo", ttl=3600), "lifetime too long"),
        (web_token(42, "octo", aud="something-else"), "Invalid token"),
        (web_token(42, "octo", iss="someone-else"), "Invalid token"),
        (web_token(0, "octo"), "Invalid token claims"),
        (web_token(42, ""), "Invalid token claims"),
        ("not.a.jwt", "Invalid token"),
    ],
)
def test_bad_internal_tokens(token: str, message: str) -> None:
    with pytest.raises(AuthError, match=message):
        verify_internal_token(token, _settings())


def test_alg_none_is_rejected() -> None:
    now = int(time.time())
    unsigned = jwt.encode(
        {
            "iss": "groundcrew-web",
            "aud": "groundcrew-api",
            "sub": "42",
            "login": "o",
            "iat": now,
            "exp": now + 60,
        },
        key="",
        algorithm="none",
    )
    with pytest.raises(AuthError):
        verify_internal_token(unsigned, _settings())


def test_missing_or_short_secret_is_a_config_error() -> None:
    with pytest.raises(AuthConfigError):
        verify_internal_token(web_token(42, "octo"), _settings(internal_jwt_secret=None))
    with pytest.raises(AuthConfigError):
        verify_internal_token(
            web_token(42, "octo"), _settings(internal_jwt_secret=SecretStr("short"))
        )


async def test_routes_require_a_valid_token(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health")).status_code == 200
    for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer nope"}):
        resp = await client.get("/me", headers=headers)
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"
    expired = auth_headers(42, "octo", issued_at=int(time.time()) - 3600)
    assert (await client.get("/me", headers=expired)).status_code == 401


async def test_unconfigured_secret_is_503(client: httpx.AsyncClient) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(internal_jwt_secret=None)
    assert (await client.get("/me", headers=auth_headers(42, "octo"))).status_code == 503


# --- users and first-login workspace --------------------------------------------------------


async def test_first_login_creates_user_and_workspace_once(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    for _ in range(2):
        resp = await client.get("/me", headers=auth_headers(42, "octo"))
        assert resp.status_code == 200
    async with session_factory() as session:
        users = (await session.scalars(select(User))).all()
        workspaces = (await session.scalars(select(Workspace))).all()
        members = (await session.scalars(select(WorkspaceMember))).all()
    assert [(u.github_id, u.github_login) for u in users] == [(42, "octo")]
    assert [w.name for w in workspaces] == ["octo's workspace"]
    assert [(m.user_id, m.role) for m in members] == [(users[0].id, WorkspaceRole.owner)]


async def test_profile_changes_are_saved(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    await client.get("/me", headers=auth_headers(42, "octo"))
    me = (
        await client.get(
            "/me", headers=auth_headers(42, "octocat", name="Mona", avatar_url="https://a/new")
        )
    ).json()
    assert (me["user"]["github_login"], me["user"]["name"], me["user"]["avatar_url"]) == (
        "octocat",
        "Mona",
        "https://a/new",
    )
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Workspace.id))) == 1


async def test_email_used_by_another_account_is_not_copied(
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session:
        first = await upsert_user(
            session, WebIdentity(github_id=1, login="one", email="shared@example.com")
        )
        second = await upsert_user(
            session, WebIdentity(github_id=2, login="two", email="shared@example.com")
        )
    assert first.email == "shared@example.com" and second.email is None


# --- stream tokens ---------------------------------------------------------------------------


async def test_stream_token_round_trip() -> None:
    user_id, run_id = uuid.uuid4(), uuid.uuid4()
    token, expires = create_stream_token(user_id, run_id, _settings())
    assert verify_stream_token(token, run_id, _settings()) == user_id
    assert expires - datetime.now(UTC) <= timedelta(seconds=61)


def test_stream_token_for_another_run_is_rejected() -> None:
    token, _ = create_stream_token(uuid.uuid4(), uuid.uuid4(), _settings())
    with pytest.raises(AuthError, match="different run"):
        verify_stream_token(token, uuid.uuid4(), _settings())


def test_expired_stream_token_is_rejected() -> None:
    run_id = uuid.uuid4()
    token, _ = create_stream_token(
        uuid.uuid4(), run_id, _settings(), now=datetime.now(UTC) - timedelta(minutes=5)
    )
    with pytest.raises(AuthError, match="expired"):
        verify_stream_token(token, run_id, _settings())


def test_internal_token_is_not_a_stream_token() -> None:
    with pytest.raises(AuthError):
        verify_stream_token(web_token(42, "octo"), uuid.uuid4(), _settings())


async def test_events_endpoint_checks_the_stream_token(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    alice = auth_headers(1, "alice")
    bob = auth_headers(2, "bob")
    me = (await client.get("/me", headers=alice)).json()
    (await client.get("/me", headers=bob)).json()
    async with session_factory() as session:
        run = Run(
            workspace_id=uuid.UUID(me["workspaces"][0]["id"]),
            agent="echo_ops",
            goal="g",
            status=RunStatus.succeeded,
        )
        other = Run(
            workspace_id=uuid.UUID(me["workspaces"][0]["id"]),
            agent="echo_ops",
            goal="g",
            status=RunStatus.succeeded,
        )
        session.add_all([run, other])
        await session.commit()
        bob_user = await session.scalar(select(User).where(User.github_login == "bob"))
    assert bob_user is not None

    good = (await client.post(f"/runs/{run.id}/stream-token", headers=alice)).json()["token"]
    assert (await client.get(f"/runs/{run.id}/events", params={"token": good})).status_code == 200
    # Missing token, wrong run, expired, and a token minted for a non-member.
    assert (await client.get(f"/runs/{run.id}/events")).status_code == 422
    assert (await client.get(f"/runs/{other.id}/events", params={"token": good})).status_code == 401
    expired, _ = create_stream_token(
        uuid.UUID(me["user"]["id"]),
        run.id,
        _settings(),
        now=datetime.now(UTC) - timedelta(minutes=5),
    )
    assert (
        await client.get(f"/runs/{run.id}/events", params={"token": expired})
    ).status_code == 401
    forged_for_bob, _ = create_stream_token(bob_user.id, run.id, _settings())
    resp = await client.get(f"/runs/{run.id}/events", params={"token": forged_for_bob})
    assert resp.status_code == 404
    # Bob cannot get a token for Alice's run in the first place.
    assert (await client.post(f"/runs/{run.id}/stream-token", headers=bob)).status_code == 404
