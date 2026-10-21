"""A workspace sees and links only the GitHub App installations its owner proved access to on
GitHub; the OAuth state is bound to that person and workspace; webhooks must be signed and keep
connections in step with installs and uninstalls."""

import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.config import get_settings
from app.connectors import github_claims
from app.connectors.github import GitHubError
from app.db.models import AuditEvent, Connection, Workspace
from app.db.session import SessionFactory, get_session_factory
from app.main import app
from app.routes import connections as connections_routes
from tests.discovery_helpers import INSTALLATION, Recorded, github_app, mock_transport

from .test_api import _settings
from .test_connections_api import Member, _member, claim_installations

pytestmark = pytest.mark.anyio

SECRET = "whsec-0123456789abcdef"  # noqa: S105


def _use(**overrides: Any) -> None:
    values: dict[str, Any] = {
        "github_app_client_id": "Iv1.groundcrew",
        "github_app_client_secret": SecretStr("oauth-secret"),
        "github_webhook_secret": SecretStr(SECRET),
        "web_origin": "https://groundkrew.com",
        **overrides,
    }
    app.dependency_overrides[get_settings] = lambda: _settings(**values)


@pytest.fixture
async def client(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    fixture_app = github_app()
    monkeypatch.setattr(connections_routes, "get_github_app", lambda: fixture_app)
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    _use()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def alice(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 9801, "alice")


async def _installations(client: httpx.AsyncClient, member: Member) -> dict[str, Any]:
    r = await client.get(
        "/connections/github/installations",
        params={"workspace_id": str(member.workspace_id)},
        headers=member.headers,
    )
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def test_other_accounts_installations_stay_invisible(
    client: httpx.AsyncClient,
    session_factory: SessionFactory,
    alice: Member,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The app is installed on GroundsCrew-AI, but Alice has not shown she can reach it.
    listed = await _installations(client, alice)
    assert listed["installations"] == [] and listed["needs_authorization"] is True
    link = {
        "workspace_id": str(alice.workspace_id),
        "installation_id": INSTALLATION,
        "repos": ["GroundsCrew-AI/checkout-demo"],
    }
    r = await client.post("/connections/github", json=link, headers=alice.headers)
    assert r.status_code == 422 and "not found" in r.json()["detail"]

    # GitHub says she can reach a different installation only.
    claimed = await claim_installations(
        client, alice, monkeypatch, [{"id": 999, "account": "alice"}]
    )
    assert claimed.status_code == 200
    assert [i["account"] for i in claimed.json()["installations"]] == ["alice"]
    r = await client.post("/connections/github", json=link, headers=alice.headers)
    assert r.status_code == 422

    async with session_factory() as session:
        event = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "github.installations_claimed")
        )
    assert event is not None and event.detail == {"accounts": ["alice"]}


async def test_the_state_belongs_to_one_person_and_workspace(
    client: httpx.AsyncClient,
    session_factory: SessionFactory,
    alice: Member,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bob = await _member(session_factory, 9802, "bob")
    started = await client.get(
        "/connections/github/authorize",
        params={"workspace_id": str(alice.workspace_id)},
        headers=alice.headers,
    )
    url = urlparse(started.json()["authorize_url"])
    query = parse_qs(url.query)
    assert url.netloc == "github.com" and url.path == "/login/oauth/authorize"
    assert query["client_id"] == ["Iv1.groundcrew"]
    assert query["redirect_uri"] == ["https://groundkrew.com/connections/github/callback"]
    state = query["state"][0]

    async def fake(*_: Any, **__: Any) -> list[dict[str, Any]]:
        return [{"id": INSTALLATION, "account": "GroundsCrew-AI"}]

    monkeypatch.setattr(github_claims, "user_installations", fake)
    stolen = await client.post(
        "/connections/github/claim", json={"code": "c", "state": state}, headers=bob.headers
    )
    assert stolen.status_code == 422 and "someone else" in stolen.json()["detail"]
    forged = await client.post(
        "/connections/github/claim",
        json={"code": "c", "state": state[:-4] + "AAAA"},
        headers=alice.headers,
    )
    assert forged.status_code == 422
    # Bob cannot start one for Alice's workspace either.
    r = await client.get(
        "/connections/github/authorize",
        params={"workspace_id": str(alice.workspace_id)},
        headers=bob.headers,
    )
    assert r.status_code == 404
    async with session_factory() as session:
        ws = await session.get_one(Workspace, bob.workspace_id)
        assert github_claims.CLAIMS_KEY not in (ws.settings or {})


async def test_the_code_exchange_keeps_no_token() -> None:
    recorded = Recorded()
    routes = {
        "POST /login/oauth/access_token": {"access_token": "ghu_userToken", "token_type": "bearer"},
        "GET /user/installations": {
            "total_count": 1,
            "installations": [{"id": 7, "account": {"login": "acme"}}],
        },
    }
    s = _settings(
        github_app_client_id="Iv1.x",
        github_app_client_secret=SecretStr("sec"),
        web_origin="https://groundkrew.com",
    )
    found = await github_claims.user_installations(
        s, "abc", transport=mock_transport(routes, recorded)
    )
    assert found == [{"id": 7, "account": "acme"}]
    exchange, listing = recorded.requests
    assert exchange.url.host == "github.com" and b"code=abc" in exchange.content
    assert (
        b"redirect_uri=https%3A%2F%2Fgroundkrew.com%2Fconnections%2Fgithub%2Fcallback"
        in exchange.content
    )
    assert listing.headers["authorization"] == "Bearer ghu_userToken"

    bad = {"POST /login/oauth/access_token": {"error": "bad_verification_code"}}
    with pytest.raises(GitHubError, match="did not accept"):
        await github_claims.user_installations(s, "abc", transport=mock_transport(bad))


def _signed(
    payload: dict[str, Any], event: str, secret: str = SECRET
) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, {
        "x-github-event": event,
        "x-hub-signature-256": sig,
        "x-github-delivery": str(uuid.uuid4()),
        "content-type": "application/json",
    }


async def test_webhooks_must_be_signed(client: httpx.AsyncClient) -> None:
    body, headers = _signed({"zen": "hi"}, "ping")
    assert (await client.post("/github/webhook", content=body, headers=headers)).status_code == 202
    body, headers = _signed({"zen": "hi"}, "ping", secret="wrong")  # noqa: S106
    assert (await client.post("/github/webhook", content=body, headers=headers)).status_code == 401
    unsigned = {k: v for k, v in headers.items() if k != "x-hub-signature-256"}
    assert (await client.post("/github/webhook", content=body, headers=unsigned)).status_code == 401
    _use(github_webhook_secret=None)
    assert (await client.post("/github/webhook", content=body, headers=headers)).status_code == 503


async def test_uninstall_suspend_and_removed_repositories(
    client: httpx.AsyncClient,
    session_factory: SessionFactory,
    alice: Member,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await claim_installations(
        client, alice, monkeypatch, [{"id": INSTALLATION, "account": "GroundsCrew-AI"}]
    )
    link = {
        "workspace_id": str(alice.workspace_id),
        "installation_id": INSTALLATION,
        "repos": ["GroundsCrew-AI/checkout-demo", "GroundsCrew-AI/groundcrew"],
    }
    conn_id = (await client.post("/connections/github", json=link, headers=alice.headers)).json()[
        "id"
    ]
    inst = {"id": INSTALLATION, "account": {"login": "GroundsCrew-AI"}}

    body, headers = _signed(
        {
            "action": "removed",
            "installation": inst,
            "repositories_removed": [{"full_name": "GroundsCrew-AI/groundcrew"}],
        },
        "installation_repositories",
    )
    r = await client.post("/github/webhook", content=body, headers=headers)
    assert r.json()["detail"] == "1 connections lost 1 repositories"

    body, headers = _signed({"action": "suspend", "installation": inst}, "installation")
    await client.post("/github/webhook", content=body, headers=headers)
    async with session_factory() as session:
        conn = await session.get_one(Connection, uuid.UUID(conn_id))
        assert conn.status == "error" and "suspended" in conn.config["last_error"]
        assert conn.config["repos"] == ["GroundsCrew-AI/checkout-demo"]
    body, headers = _signed({"action": "unsuspend", "installation": inst}, "installation")
    await client.post("/github/webhook", content=body, headers=headers)

    body, headers = _signed({"action": "deleted", "installation": inst}, "installation")
    r = await client.post("/github/webhook", content=body, headers=headers)
    assert r.json() == {
        "handled": True,
        "detail": "1 connections stopped, 1 workspaces released it",
    }
    async with session_factory() as session:
        conn = await session.get_one(Connection, uuid.UUID(conn_id))
        assert (
            conn.status == "error"
            and "uninstalled from GroundsCrew-AI" in conn.config["last_error"]
        )
        ws = await session.get_one(Workspace, alice.workspace_id)
        assert ws.settings[github_claims.CLAIMS_KEY] == {}
