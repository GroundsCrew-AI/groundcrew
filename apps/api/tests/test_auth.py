"""Accounts and auth: internal JWTs, the web server's service token, GitHub and Google sign-in
and linking, email and password sign-up with confirmation, password reset, lockout, ended
sessions, invites by email, and SSE stream tokens."""

import re
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

import httpx
import jwt
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from app.config import Settings, get_settings
from app.db.models import (
    Run,
    RunStatus,
    User,
    UserIdentity,
    Workspace,
    WorkspaceMember,
    WorkspaceRole,
)
from app.db.session import SessionFactory, get_session_factory
from app.mail import MemoryMailer, get_mailer
from app.main import app
from app.security.auth import (
    AuthConfigError,
    AuthError,
    WebIdentity,
    create_stream_token,
    verify_internal_token,
    verify_stream_token,
)
from app.security.ratelimit import limiter
from tests.auth_helpers import (
    TEST_JWT_SECRET,
    auth_headers,
    github_user,
    service_headers,
    web_token,
)

pytestmark = pytest.mark.anyio

PASSWORD = "correct horse battery"  # noqa: S105


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "internal_jwt_secret": SecretStr(TEST_JWT_SECRET),
        "web_origin": "https://app.example.com",
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
    limiter.reset()
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = lambda: _settings()
    app.dependency_overrides[get_mailer] = lambda: mailer
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    limiter.reset()


def _link_token(mailer: MemoryMailer, path: str) -> str:
    match = re.search(rf"{re.escape(path)}\?token=([^\s&]+)", mailer.outbox[-1].text)
    assert match, mailer.outbox[-1].text
    return unquote(match.group(1))


async def _auth(client: httpx.AsyncClient, path: str, body: dict[str, Any]) -> httpx.Response:
    return await client.post(f"/auth/{path}", json=body, headers=service_headers())


async def _oauth(client: httpx.AsyncClient, **profile: Any) -> dict[str, Any]:
    r = await _auth(client, "oauth", profile)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def _signed_up(client: httpx.AsyncClient, mailer: MemoryMailer, email: str) -> str:
    """A confirmed password account; returns its id."""
    r = await _auth(client, "sign-up", {"email": email, "password": PASSWORD, "name": "Ada"})
    assert r.status_code == 202, r.text
    token = _link_token(mailer, "/verify-email")
    r = await _auth(client, "verify-email", {"token": token, "password": PASSWORD})
    assert r.status_code == 200, r.text
    user_id: str = r.json()["id"]
    return user_id


# --- internal JWT -------------------------------------------------------------------------


def test_valid_internal_token() -> None:
    uid = uuid.uuid4()
    identity = verify_internal_token(web_token(uid, auth_time=1700000000), _settings())
    assert identity == WebIdentity(user_id=uid, auth_time=1700000000)


@pytest.mark.parametrize(
    ("token", "message"),
    [
        (web_token(uuid.uuid4(), secret="another-secret-that-is-long-enough!!"), "Invalid token"),  # noqa: S106
        (web_token(uuid.uuid4(), issued_at=int(time.time()) - 3600), "expired"),
        (web_token(uuid.uuid4(), ttl=3600), "lifetime too long"),
        (web_token(uuid.uuid4(), aud="something-else"), "Invalid token"),
        (web_token(uuid.uuid4(), iss="someone-else"), "Invalid token"),
        (web_token("42"), "Invalid token claims"),
        (web_token(uuid.uuid4(), auth_time=-1), "Invalid token claims"),
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
            "sub": str(uuid.uuid4()),
            "auth_time": now,
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
        verify_internal_token(web_token(uuid.uuid4()), _settings(internal_jwt_secret=None))
    with pytest.raises(AuthConfigError):
        verify_internal_token(
            web_token(uuid.uuid4()), _settings(internal_jwt_secret=SecretStr("short"))
        )


async def test_routes_require_a_valid_token(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    assert (await client.get("/health")).status_code == 200
    for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer nope"}):
        resp = await client.get("/me", headers=headers)
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"
    user = await github_user(session_factory, 42, "octo")
    expired = auth_headers(user.id, issued_at=int(time.time()) - 3600)
    assert (await client.get("/me", headers=expired)).status_code == 401
    # A valid token for a user that does not exist (deleted) is refused too.
    assert (await client.get("/me", headers=auth_headers(uuid.uuid4()))).status_code == 401
    assert (await client.get("/me", headers=auth_headers(user.id))).status_code == 200


async def test_unconfigured_secret_is_503(client: httpx.AsyncClient) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(internal_jwt_secret=None)
    assert (await client.get("/me", headers=auth_headers(uuid.uuid4()))).status_code == 503


async def test_sign_in_endpoints_take_only_the_service_token(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    user = await github_user(session_factory, 42, "octo")
    profile = {"provider": "github", "account_id": "43", "login": "mallory"}
    # No token, a user's token, or a service token signed with another secret: refused.
    for headers in ({}, auth_headers(user.id), service_headers(secret="x" * 40)):
        r = await client.post("/auth/oauth", json=profile, headers=headers)
        assert r.status_code == 401
    # And the service token is not a user's token.
    assert (await client.get("/me", headers=service_headers())).status_code == 401
    assert (await _auth(client, "oauth", profile)).status_code == 200


# --- GitHub and Google ------------------------------------------------------------------------


async def test_first_sign_in_creates_user_and_workspace_once(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    for _ in range(2):
        user = await _oauth(
            client, provider="github", account_id="42", login="octo", name="Octo Cat"
        )
    async with session_factory() as session:
        users = (await session.scalars(select(User))).all()
        workspaces = (await session.scalars(select(Workspace))).all()
        members = (await session.scalars(select(WorkspaceMember))).all()
        identities = (await session.scalars(select(UserIdentity))).all()
    assert [(u.github_id, u.github_login) for u in users] == [(42, "octo")]
    assert [w.name for w in workspaces] == ["Octo Cat's workspace"]
    assert [(m.user_id, m.role) for m in members] == [(users[0].id, WorkspaceRole.owner)]
    assert [(i.provider, i.provider_account_id) for i in identities] == [("github", "42")]
    assert user["id"] == str(users[0].id)
    me = (await client.get("/me", headers=auth_headers(user["id"]))).json()
    assert me["user"]["sign_in_methods"] == ["github"] and me["user"]["has_password"] is False


async def test_profile_changes_are_saved_but_a_chosen_name_is_kept(
    client: httpx.AsyncClient,
) -> None:
    user = await _oauth(client, provider="github", account_id="42", login="octo")
    headers = auth_headers(user["id"])
    assert (await client.patch("/me", json={"name": "Mona"}, headers=headers)).status_code == 200
    await _oauth(
        client,
        provider="github",
        account_id="42",
        login="octocat",
        name="Octo",
        avatar_url="https://a/new",
    )
    me = (await client.get("/me", headers=headers)).json()["user"]
    assert (me["github_login"], me["name"], me["avatar_url"]) == (
        "octocat",
        "Mona",
        "https://a/new",
    )


async def test_unverified_email_is_never_linked_or_copied(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    first = await _oauth(
        client, provider="github", account_id="1", login="one", email="shared@example.com",
        email_verified=True,
    )  # fmt: skip
    second = await _oauth(
        client, provider="google", account_id="g-2", email="shared@example.com",
        email_verified=False, name="Two",
    )  # fmt: skip
    assert first["id"] != second["id"]
    assert first["email"] == "shared@example.com" and second["email"] is None


async def test_google_links_to_the_account_with_that_verified_address(
    client: httpx.AsyncClient, mailer: MemoryMailer
) -> None:
    ada = await _signed_up(client, mailer, "ada@example.com")
    google = await _oauth(
        client, provider="google", account_id="g-1", email="ADA@example.com",
        email_verified=True, name="Ada L",
    )  # fmt: skip
    assert google["id"] == ada
    me = (await client.get("/me", headers=auth_headers(ada))).json()
    assert me["user"]["sign_in_methods"] == ["password", "google"]
    assert len(me["workspaces"]) == 1


async def test_a_provider_takes_an_address_back_from_an_unconfirmed_sign_up(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    # Someone signs up with Ada's address but cannot confirm it.
    r = await _auth(client, "sign-up", {"email": "ada@example.com", "password": PASSWORD})
    assert r.status_code == 202
    async with session_factory() as session:
        squatter = await session.scalar(select(User).where(User.email == "ada@example.com"))
    assert squatter is not None
    old_session = auth_headers(squatter.id, auth_time=int(time.time()) - 60)
    # Ada signs in with Google, which verified the address: the password is gone.
    ada = await _oauth(
        client, provider="google", account_id="g-ada", email="ada@example.com",
        email_verified=True,
    )  # fmt: skip
    assert ada["id"] == str(squatter.id) and ada["email_verified"] is True
    r = await _auth(client, "sign-in", {"email": "ada@example.com", "password": PASSWORD})
    assert r.status_code == 401
    assert (await client.get("/me", headers=old_session)).status_code == 401


# --- email and password -----------------------------------------------------------------------


async def test_sign_up_confirm_and_sign_in(
    client: httpx.AsyncClient, mailer: MemoryMailer, session_factory: SessionFactory
) -> None:
    r = await _auth(
        client, "sign-up", {"email": " Ada@Example.com ", "password": PASSWORD, "name": "Ada"}
    )
    assert r.status_code == 202 and r.json() == {"status": "check_email"}
    assert mailer.outbox[-1].to == "ada@example.com"
    assert "https://app.example.com/verify-email?token=" in mailer.outbox[-1].text

    # Not before the address is confirmed.
    r = await _auth(client, "sign-in", {"email": "ada@example.com", "password": PASSWORD})
    assert r.status_code == 403 and r.json()["code"] == "unverified"
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Workspace.id))) == 0

    token = _link_token(mailer, "/verify-email")
    # The link alone is not enough: it needs the account's password.
    wrong = await _auth(client, "verify-email", {"token": token, "password": "not it at all"})
    assert wrong.status_code == 401
    r = await _auth(client, "verify-email", {"token": token, "password": PASSWORD})
    assert r.status_code == 200 and r.json()["email_verified"] is True
    again = await _auth(client, "verify-email", {"token": token, "password": PASSWORD})
    assert again.status_code == 400 and again.json()["code"] == "invalid_link"

    r = await _auth(client, "sign-in", {"email": "ADA@example.com", "password": PASSWORD})
    assert r.status_code == 200, r.text
    me = (await client.get("/me", headers=auth_headers(r.json()["id"]))).json()
    assert [w["name"] for w in me["workspaces"]] == ["Ada's workspace"]
    assert me["user"]["has_password"] is True and me["user"]["email_verified"] is True


async def test_sign_up_does_not_reveal_existing_accounts(
    client: httpx.AsyncClient, mailer: MemoryMailer, session_factory: SessionFactory
) -> None:
    await _signed_up(client, mailer, "ada@example.com")
    r = await _auth(client, "sign-up", {"email": "ada@example.com", "password": "another one 123"})
    assert r.status_code == 202 and r.json() == {"status": "check_email"}
    assert mailer.outbox[-1].subject == "You already have a Groundcrew account"
    async with session_factory() as session:
        assert await session.scalar(select(func.count(User.id))) == 1
    # The password did not change.
    r = await _auth(client, "sign-in", {"email": "ada@example.com", "password": PASSWORD})
    assert r.status_code == 200


async def test_weak_passwords_and_bad_input(client: httpx.AsyncClient) -> None:
    r = await _auth(client, "sign-up", {"email": "ada@example.com", "password": "short"})
    assert r.status_code == 422 and r.json()["code"] == "weak_password"
    r = await _auth(client, "sign-up", {"email": "ada@example.com", "password": "password1234"})
    assert r.status_code == 422 and "guess" in r.json()["detail"]
    r = await _auth(client, "sign-up", {"email": "not-an-email", "password": PASSWORD})
    assert r.status_code == 422
    # A 422 never echoes the password back.
    r = await _auth(client, "sign-in", {"email": "ada@example.com", "password": "p" * 500})
    assert r.status_code == 422 and "ppppp" not in r.text


async def test_wrong_passwords_lock_the_account(
    client: httpx.AsyncClient, mailer: MemoryMailer
) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(auth_lockout_attempts=3)
    await _signed_up(client, mailer, "ada@example.com")
    for _ in range(3):
        r = await _auth(client, "sign-in", {"email": "ada@example.com", "password": "nope nope 1"})
        assert r.status_code == 401 and r.json()["detail"] == "Email or password is incorrect."
    r = await _auth(client, "sign-in", {"email": "ada@example.com", "password": PASSWORD})
    assert r.status_code == 429 and r.json()["code"] == "locked"
    # An unknown address answers like a wrong password.
    r = await _auth(client, "sign-in", {"email": "who@example.com", "password": PASSWORD})
    assert r.status_code == 401 and r.json()["code"] == "invalid"


async def test_password_reset_ends_other_sessions(
    client: httpx.AsyncClient, mailer: MemoryMailer
) -> None:
    ada = await _signed_up(client, mailer, "ada@example.com")
    old = auth_headers(ada, auth_time=int(time.time()) - 60)
    assert (await client.get("/me", headers=old)).status_code == 200

    sent = len(mailer.outbox)
    r = await _auth(client, "forgot-password", {"email": "nobody@example.com"})
    assert r.status_code == 202 and len(mailer.outbox) == sent  # nothing to send, same answer
    r = await _auth(client, "forgot-password", {"email": "ada@example.com"})
    assert r.status_code == 202 and len(mailer.outbox) == sent + 1
    token = _link_token(mailer, "/reset-password")

    new = "a brand new passphrase"
    r = await _auth(client, "reset-password", {"token": token, "password": new})
    assert r.status_code == 200 and r.json()["id"] == ada
    assert (await client.get("/me", headers=old)).status_code == 401
    r = await _auth(client, "reset-password", {"token": token, "password": "yet another one 9"})
    assert r.status_code == 400  # single use
    r = await _auth(client, "sign-in", {"email": "ada@example.com", "password": PASSWORD})
    assert r.status_code == 401
    r = await _auth(client, "sign-in", {"email": "ada@example.com", "password": new})
    assert r.status_code == 200
    assert (await client.get("/me", headers=auth_headers(ada))).status_code == 200


async def test_change_password_from_account_settings(
    client: httpx.AsyncClient, mailer: MemoryMailer
) -> None:
    ada = await _signed_up(client, mailer, "ada@example.com")
    headers = auth_headers(ada, auth_time=int(time.time()) - 5)
    r = await client.post(
        "/me/password",
        json={"current_password": "wrong one here", "new_password": "a brand new passphrase"},
        headers=headers,
    )
    assert r.status_code == 400
    r = await client.post(
        "/me/password",
        json={"current_password": PASSWORD, "new_password": "a brand new passphrase"},
        headers=headers,
    )
    assert r.status_code == 204
    # Every session ends, this one included.
    assert (await client.get("/me", headers=headers)).status_code == 401


async def test_github_accounts_can_add_a_password(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    user = await _oauth(
        client, provider="github", account_id="7", login="gh", email="gh@example.com",
        email_verified=True,
    )  # fmt: skip
    headers = auth_headers(user["id"], auth_time=int(time.time()) - 5)
    r = await client.post("/me/password", json={"new_password": PASSWORD}, headers=headers)
    assert r.status_code == 204
    r = await _auth(client, "sign-in", {"email": "gh@example.com", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["id"] == user["id"]


async def test_sign_in_attempts_are_rate_limited(client: httpx.AsyncClient) -> None:
    codes = []
    for _ in range(12):
        r = await _auth(client, "sign-in", {"email": "who@example.com", "password": "x" * 12})
        codes.append(r.status_code)
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]


async def test_email_sign_in_can_be_turned_off(client: httpx.AsyncClient) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(password_auth_enabled=False)
    r = await _auth(client, "sign-up", {"email": "ada@example.com", "password": PASSWORD})
    assert r.status_code == 403 and r.json()["code"] == "disabled"
    options = await client.get("/auth/options", headers=service_headers())
    assert options.json()["password"] is False


# --- invites by email -------------------------------------------------------------------------


async def test_email_invite_applies_when_the_address_is_confirmed(
    client: httpx.AsyncClient, mailer: MemoryMailer, session_factory: SessionFactory
) -> None:
    owner = await github_user(session_factory, 500, "olivia", name="Olivia")
    async with session_factory() as session:
        ws = await session.scalar(select(Workspace.id).where(Workspace.owner_id == owner.id))
    r = await client.post(
        f"/workspaces/{ws}/members", json={"email": "Pat@Example.com"},
        headers=auth_headers(owner.id),
    )  # fmt: skip
    assert r.status_code == 200, r.text
    assert [i["email"] for i in r.json()["invites"]] == ["pat@example.com"]
    assert mailer.outbox[-1].to == "pat@example.com"
    assert "Olivia invited you" in mailer.outbox[-1].subject
    assert "https://app.example.com/signup?email=pat%40example.com" in mailer.outbox[-1].text

    # Signing up is not enough; confirming the address is.
    await _auth(client, "sign-up", {"email": "pat@example.com", "password": PASSWORD})
    listed = await client.get(f"/workspaces/{ws}/members", headers=auth_headers(owner.id))
    assert len(listed.json()["invites"]) == 1
    token = _link_token(mailer, "/verify-email")
    pat = (await _auth(client, "verify-email", {"token": token, "password": PASSWORD})).json()
    me = (await client.get("/me", headers=auth_headers(pat["id"]))).json()
    assert {w["id"] for w in me["workspaces"]} >= {str(ws)}
    listed = await client.get(f"/workspaces/{ws}/members", headers=auth_headers(owner.id))
    assert listed.json()["invites"] == []
    # A confirmed address joins at once.
    r = await client.post(
        f"/workspaces/{ws}/members", json={"email": "pat@example.com"},
        headers=auth_headers(owner.id),
    )  # fmt: skip
    assert r.status_code == 409


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
        verify_stream_token(web_token(uuid.uuid4()), uuid.uuid4(), _settings())


async def test_events_endpoint_checks_the_stream_token(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    alice_user = await github_user(session_factory, 1, "alice")
    bob_user = await github_user(session_factory, 2, "bob")
    alice, bob = auth_headers(alice_user.id), auth_headers(bob_user.id)
    me = (await client.get("/me", headers=alice)).json()
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
