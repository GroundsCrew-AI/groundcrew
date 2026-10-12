"""HTTP API for connections, discovery, and the graph. External services are faked: AWS STS
through a stand-in session cache, GitHub and Vercel through recorded responses."""

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

import app.routes.connections as connections_routes
from app.accounts import upsert_user
from app.config import Settings, get_settings
from app.connectors.aws import AwsError, CallerIdentity
from app.connectors.vercel import VercelClient
from app.db.models import Connection, Job, Resource, Run, Workspace
from app.db.session import SessionFactory, get_session_factory
from app.discovery.engine import DiscoveryEngine, start_discovery
from app.discovery.types import ConnectorError
from app.main import app
from app.security.auth import WebIdentity
from app.security.crypto import decrypt, generate_key
from tests.auth_helpers import TEST_JWT_SECRET, auth_headers
from tests.discovery_helpers import FAKE_VERCEL_TOKEN, github_app, load, mock_transport
from tests.fakes import FakeLLM, reply
from tests.test_discovery_engine import SUMMARY, Inventory

pytestmark = pytest.mark.anyio

PRINCIPAL = "arn:aws:iam::881050425883:user/groundcrew-demo"
ROLE = "arn:aws:iam::881050425883:role/groundcrew-read-only"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "internal_jwt_secret": SecretStr(TEST_JWT_SECRET),
        "model_nano": "test/nano",
        "discovery_regions": ["us-east-2"],
        **overrides,
    }
    return Settings(_env_file=None, **values)


class FakeSessions:
    def __init__(self) -> None:
        self.verified: list[tuple[str, str]] = []
        self.fail: str | None = None
        # Roles that cannot be assumed (a stack without Tier=deploy has no deploy role).
        self.missing: set[str] = set()
        self.forgotten: list[str] = []

    def principal_arn(self) -> str:
        return PRINCIPAL

    def verify(self, role_arn: str, external_id: str) -> CallerIdentity:
        self.verified.append((role_arn, external_id))
        if self.fail:
            raise AwsError(self.fail)
        if role_arn in self.missing:
            raise AwsError(f"Could not assume {role_arn}: AccessDenied")
        return CallerIdentity(
            account="881050425883",
            arn="arn:aws:sts::881050425883:assumed-role/groundcrew-read-only/x",
        )

    def forget(self, role_arn: str, _external_id: str) -> None:
        self.forgotten.append(role_arn)


@pytest.fixture(autouse=True)
def secrets_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SECRETS_KEY", generate_key())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> FakeSessions:
    fake = FakeSessions()
    monkeypatch.setattr(connections_routes, "get_sessions", lambda: fake)
    return fake


@pytest.fixture
def overrides() -> dict[str, Any]:
    return {}


@pytest.fixture
async def client(
    session_factory: SessionFactory, overrides: dict[str, Any]
) -> AsyncIterator[httpx.AsyncClient]:
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = lambda: _settings(**overrides)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.dependency_overrides.clear()


@dataclass
class Member:
    workspace_id: uuid.UUID
    headers: dict[str, str]


async def _member(sf: SessionFactory, github_id: int, login: str) -> Member:
    async with sf() as s:
        user = await upsert_user(s, WebIdentity(github_id=github_id, login=login))
        ws = await s.scalar(select(Workspace).where(Workspace.owner_id == user.id))
        assert ws is not None
        await s.commit()
        return Member(ws.id, auth_headers(github_id, login))


@pytest.fixture
async def alice(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 2001, "alice")


@pytest.fixture
async def mallory(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 2002, "mallory")


# --- AWS ----------------------------------------------------------------------------------


async def test_aws_start_gives_external_id_and_cli_without_template_url(
    client: httpx.AsyncClient, alice: Member, sessions: FakeSessions
) -> None:
    r = await client.post(
        "/connections/aws/start",
        json={"workspace_id": str(alice.workspace_id)},
        headers=alice.headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["principal_arn"] == PRINCIPAL and body["region"] == "us-east-2"
    assert body["external_id"].startswith("gc-") and body["console_url"] is None
    assert f"ParameterValue={body['external_id']}" in body["cli_command"]
    assert "--region us-east-2" in body["cli_command"]
    # Starting again reuses the pending connection and its ExternalId.
    again = (
        await client.post(
            "/connections/aws/start",
            json={"workspace_id": str(alice.workspace_id)},
            headers=alice.headers,
        )
    ).json()
    assert (
        again["external_id"] == body["external_id"]
        and again["connection_id"] == body["connection_id"]
    )


@pytest.mark.parametrize(
    "overrides",
    [{"aws_connect_template_url": "https://s3.us-east-2.amazonaws.com/gc/aws-connect.yaml"}],
)
async def test_aws_start_console_link_is_prefilled_in_us_east_2(
    client: httpx.AsyncClient, alice: Member, sessions: FakeSessions
) -> None:
    body = (
        await client.post(
            "/connections/aws/start",
            json={"workspace_id": str(alice.workspace_id)},
            headers=alice.headers,
        )
    ).json()
    url = body["console_url"]
    assert url.startswith(
        "https://us-east-2.console.aws.amazon.com/cloudformation/home?region=us-east-2#/stacks/quickcreate?"
    )
    assert f"param_ExternalId={body['external_id']}" in url
    assert (
        "param_GroundcrewPrincipalArn=arn%3Aaws%3Aiam%3A%3A881050425883%3Auser%2Fgroundcrew-demo"
        in url
    )
    assert "templateURL=https%3A%2F%2Fs3.us-east-2.amazonaws.com" in url
    assert "stackName=groundcrew-read-only" in url and "param_RoleName=groundcrew-read-only" in url
    assert "param_Tier=observe" in url and body["tier"] == "observe"


async def test_aws_complete_verifies_and_stores_no_credentials(
    client: httpx.AsyncClient,
    alice: Member,
    sessions: FakeSessions,
    session_factory: SessionFactory,
) -> None:
    start = (
        await client.post(
            "/connections/aws/start",
            json={"workspace_id": str(alice.workspace_id)},
            headers=alice.headers,
        )
    ).json()
    r = await client.post(
        "/connections/aws/complete",
        json={"connection_id": start["connection_id"], "role_arn": ROLE},
        headers=alice.headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert (
        body["status"] == "connected"
        and body["account"] == "881050425883"
        and body["role_arn"] == ROLE
    )
    assert sessions.verified == [(ROLE, start["external_id"])]
    async with session_factory() as s:
        conn = await s.get(Connection, uuid.UUID(start["connection_id"]))
        assert conn is not None and conn.secret_encrypted is None
        assert set(conn.config) <= {
            "external_id",
            "principal_arn",
            "region",
            "role_arn",
            "account_id",
            "session_arn",
            "tier",
            "deploy_role_arn",
            "last_error",
        }
        assert conn.config["tier"] == "observe" and conn.config["deploy_role_arn"] is None
    assert body["tier"] == "observe" and body["deploy_role_arn"] is None
    template = await client.get("/connections/aws/template", headers=alice.headers)
    assert template.status_code == 200 and "sts:ExternalId" in template.text


DEPLOY_ROLE = "arn:aws:iam::881050425883:role/groundcrew-deploy"


async def _start(client: httpx.AsyncClient, member: Member, tier: str) -> dict[str, Any]:
    r = await client.post(
        "/connections/aws/start",
        json={"workspace_id": str(member.workspace_id), "tier": tier},
        headers=member.headers,
    )
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


@pytest.mark.parametrize(
    "overrides",
    [{"aws_connect_template_url": "https://s3.us-east-2.amazonaws.com/gc/aws-connect.yaml"}],
)
async def test_aws_deploy_tier_link_and_cli_carry_the_tier(
    client: httpx.AsyncClient, alice: Member, sessions: FakeSessions
) -> None:
    body = await _start(client, alice, "deploy")
    assert body["tier"] == "deploy" and "param_Tier=deploy" in body["console_url"]
    assert "ParameterKey=Tier,ParameterValue=deploy" in body["cli_command"]
    # Resuming the same pending connection with the other tier switches it.
    again = await _start(client, alice, "observe")
    assert (
        again["connection_id"] == body["connection_id"]
        and "param_Tier=observe" in again["console_url"]
    )


async def test_aws_deploy_tier_verifies_both_roles(
    client: httpx.AsyncClient,
    alice: Member,
    sessions: FakeSessions,
    session_factory: SessionFactory,
) -> None:
    start = await _start(client, alice, "deploy")
    r = await client.post(
        "/connections/aws/complete",
        json={"connection_id": start["connection_id"], "role_arn": ROLE},
        headers=alice.headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["tier"] == "deploy" and r.json()["deploy_role_arn"] == DEPLOY_ROLE
    assert sessions.verified == [(ROLE, start["external_id"]), (DEPLOY_ROLE, start["external_id"])]
    async with session_factory() as s:
        conn = await s.get(Connection, uuid.UUID(start["connection_id"]))
        assert conn is not None and conn.secret_encrypted is None


async def test_aws_deploy_tier_without_the_deploy_role_stays_pending(
    client: httpx.AsyncClient, alice: Member, sessions: FakeSessions
) -> None:
    sessions.missing.add(DEPLOY_ROLE)
    start = await _start(client, alice, "deploy")
    r = await client.post(
        "/connections/aws/complete",
        json={"connection_id": start["connection_id"], "role_arn": ROLE},
        headers=alice.headers,
    )
    assert r.status_code == 422 and "Tier=deploy" in r.json()["detail"]
    conns = (
        await client.get(
            "/connections", params={"workspace_id": str(alice.workspace_id)}, headers=alice.headers
        )
    ).json()
    (conn,) = conns
    assert conn["status"] == "pending" and conn["deploy_role_arn"] is None


async def test_aws_tier_switch(
    client: httpx.AsyncClient, alice: Member, sessions: FakeSessions
) -> None:
    start = await _start(client, alice, "observe")
    cid = start["connection_id"]
    done = await client.post(
        "/connections/aws/complete",
        json={"connection_id": cid, "role_arn": ROLE},
        headers=alice.headers,
    )
    assert done.json()["tier"] == "observe"

    sessions.missing.add(DEPLOY_ROLE)  # the stack was not updated yet
    refused = await client.post(
        f"/connections/{cid}/aws/tier", json={"tier": "deploy"}, headers=alice.headers
    )
    assert refused.status_code == 422 and "Tier parameter" in refused.json()["detail"]

    sessions.missing.clear()  # now it was
    on = await client.post(
        f"/connections/{cid}/aws/tier", json={"tier": "deploy"}, headers=alice.headers
    )
    assert on.status_code == 200 and on.json()["deploy_role_arn"] == DEPLOY_ROLE

    off = await client.post(
        f"/connections/{cid}/aws/tier", json={"tier": "observe"}, headers=alice.headers
    )
    assert off.json()["tier"] == "observe" and off.json()["deploy_role_arn"] is None
    assert DEPLOY_ROLE in sessions.forgotten


@pytest.mark.parametrize(
    "overrides,url",
    [
        ({"aws_connect_template_url": "https://gc.s3.us-east-2.amazonaws.com/aws-connect.yaml"},
         "https://gc.s3.us-east-2.amazonaws.com/aws-connect.yaml"),
        ({}, None),
    ],
)  # fmt: skip
async def test_aws_template_url(client: httpx.AsyncClient, alice: Member, url: str | None) -> None:
    r = await client.get("/connections/aws/template-url", headers=alice.headers)
    assert r.status_code == 200
    assert r.json() == {"url": url, "template_path": "/connections/aws/template"}
    assert (await client.get("/connections/aws/template-url")).status_code == 401


async def test_aws_complete_errors(
    client: httpx.AsyncClient, alice: Member, sessions: FakeSessions
) -> None:
    start = (
        await client.post(
            "/connections/aws/start",
            json={"workspace_id": str(alice.workspace_id)},
            headers=alice.headers,
        )
    ).json()
    bad = await client.post(
        "/connections/aws/complete",
        json={
            "connection_id": start["connection_id"],
            "role_arn": "arn:aws:iam::881050425883:user/x",
        },
        headers=alice.headers,
    )
    assert bad.status_code == 422 and "role ARN" in bad.json()["detail"]
    sessions.fail = "This role can be assumed without the ExternalId."
    refused = await client.post(
        "/connections/aws/complete",
        json={"connection_id": start["connection_id"], "role_arn": ROLE},
        headers=alice.headers,
    )
    assert refused.status_code == 422 and "without the ExternalId" in refused.json()["detail"]
    listed = (
        await client.get(
            "/connections", params={"workspace_id": str(alice.workspace_id)}, headers=alice.headers
        )
    ).json()
    assert listed[0]["status"] == "pending" and "ExternalId" in listed[0]["error"]


async def test_other_workspaces_get_404(
    client: httpx.AsyncClient, alice: Member, mallory: Member, sessions: FakeSessions
) -> None:
    start = (
        await client.post(
            "/connections/aws/start",
            json={"workspace_id": str(alice.workspace_id)},
            headers=alice.headers,
        )
    ).json()
    for method, path, payload in [
        ("post", "/connections/aws/start", {"workspace_id": str(alice.workspace_id)}),
        (
            "post",
            "/connections/aws/complete",
            {"connection_id": start["connection_id"], "role_arn": ROLE},
        ),
        ("delete", f"/connections/{start['connection_id']}", None),
        ("post", f"/connections/{start['connection_id']}/discover", None),
        ("post", f"/workspaces/{alice.workspace_id}/discover", None),
        ("get", f"/workspaces/{alice.workspace_id}/graph", None),
    ]:
        kwargs: dict[str, Any] = {"headers": mallory.headers}
        if payload is not None:
            kwargs["json"] = payload
        r = await getattr(client, method)(path, **kwargs)
        assert r.status_code == 404, (method, path, r.status_code)
    r = await client.get(
        "/connections", params={"workspace_id": str(alice.workspace_id)}, headers=mallory.headers
    )
    assert r.status_code == 404


# --- Vercel, GitHub, previews --------------------------------------------------------


@pytest.fixture
def vercel_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    def make(token: str, team_id: str | None, settings: Settings) -> VercelClient:
        return VercelClient(token, team_id, settings, transport=mock_transport(load("vercel.json")))

    monkeypatch.setattr(connections_routes, "VercelClient", make)


async def test_vercel_token_is_checked_and_stored_encrypted(
    client: httpx.AsyncClient, alice: Member, vercel_routes: None, session_factory: SessionFactory
) -> None:
    r = await client.post(
        "/connections/vercel",
        json={
            "workspace_id": str(alice.workspace_id),
            "token": FAKE_VERCEL_TOKEN,
            "team_id": "team_groundcrew",
        },
        headers=alice.headers,
    )
    assert r.status_code == 200, r.text
    assert FAKE_VERCEL_TOKEN not in r.text
    body = r.json()
    assert (
        body["kind"] == "vercel" and body["account"] == "ground-crew1" and body["preview"] is False
    )
    async with session_factory() as s:
        conn = await s.get(Connection, uuid.UUID(body["id"]))
        assert (
            conn is not None
            and conn.secret_encrypted
            and FAKE_VERCEL_TOKEN not in conn.secret_encrypted
        )
        assert decrypt(conn.secret_encrypted) == FAKE_VERCEL_TOKEN
        assert FAKE_VERCEL_TOKEN not in json.dumps(conn.config)


async def test_vercel_bad_team_is_422(
    client: httpx.AsyncClient, alice: Member, vercel_routes: None
) -> None:
    r = await client.post(
        "/connections/vercel",
        json={
            "workspace_id": str(alice.workspace_id),
            "token": FAKE_VERCEL_TOKEN,
            "team_id": "team_other",
        },
        headers=alice.headers,
    )
    assert r.status_code == 422 and FAKE_VERCEL_TOKEN not in r.text
    assert "cannot read team team_other" in r.json()["detail"]


async def test_vercel_rejected_token_says_what_to_do(
    client: httpx.AsyncClient, alice: Member, monkeypatch: pytest.MonkeyPatch
) -> None:
    def make(token: str, team_id: str | None, settings: Settings) -> VercelClient:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"message": "Not authorized"}})

        return VercelClient(token, team_id, settings, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(connections_routes, "VercelClient", make)
    r = await client.post(
        "/connections/vercel",
        json={"workspace_id": str(alice.workspace_id), "token": FAKE_VERCEL_TOKEN},
        headers=alice.headers,
    )
    assert r.status_code == 422 and "did not accept this token" in r.json()["detail"]


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture_app = github_app()
    monkeypatch.setattr(connections_routes, "get_github_app", lambda: fixture_app)


async def test_github_picker_and_link(client: httpx.AsyncClient, alice: Member, gh: None) -> None:
    listed = (
        await client.get(
            "/connections/github/installations",
            params={"workspace_id": str(alice.workspace_id)},
            headers=alice.headers,
        )
    ).json()
    assert (
        listed["configured"]
        and listed["install_url"] == "https://github.com/apps/groundcrew-app/installations/new"
    )
    (install,) = listed["installations"]
    assert [r["full_name"] for r in install["repos"]] == [
        "GroundsCrew-AI/checkout-demo",
        "GroundsCrew-AI/groundcrew",
    ]
    assert not any(r["linked"] for r in install["repos"])

    body = {
        "workspace_id": str(alice.workspace_id),
        "installation_id": install["id"],
        "repos": ["GroundsCrew-AI/checkout-demo"],
    }
    linked = await client.post("/connections/github", json=body, headers=alice.headers)
    assert linked.status_code == 200 and linked.json()["repos"] == ["GroundsCrew-AI/checkout-demo"]
    # Linking again updates the same connection.
    body["repos"] = ["GroundsCrew-AI/checkout-demo", "GroundsCrew-AI/groundcrew"]
    again = (await client.post("/connections/github", json=body, headers=alice.headers)).json()
    assert again["id"] == linked.json()["id"] and len(again["repos"]) == 2
    relisted = (
        await client.get(
            "/connections/github/installations",
            params={"workspace_id": str(alice.workspace_id)},
            headers=alice.headers,
        )
    ).json()
    assert all(r["linked"] for r in relisted["installations"][0]["repos"])

    body["repos"] = ["someone/else"]
    refused = await client.post("/connections/github", json=body, headers=alice.headers)
    assert refused.status_code == 422 and "cannot see" in refused.json()["detail"]


async def test_azure_is_a_preview_with_an_encrypted_secret(
    client: httpx.AsyncClient, alice: Member
) -> None:
    azure = await client.post(
        "/connections/azure",
        json={
            "workspace_id": str(alice.workspace_id),
            "tenant_id": "t",
            "client_id": "c",
            "client_secret": "azure-secret-value",
            "subscription_id": "sub-123",
            "resource_group": "rg-groundcrew-checkout",
        },
        headers=alice.headers,
    )
    assert (
        azure.status_code == 200
        and azure.json()["preview"] is True
        and "azure-secret-value" not in azure.text
    )


KUBECONFIG = (
    "apiVersion: v1\n"
    "clusters: [{name: demo, cluster: {server: 'https://1.2.3.4'}}]\n"
    "contexts: [{name: r, context: {cluster: demo, user: u}}]\n"
    "current-context: r\n"
    "users: [{name: u, user: {token: kube-token-value}}]\n"
)


class _FakeKubeReader:
    def __init__(self, kubeconfig: str, error: str | None) -> None:
        self.kubeconfig, self.error, self.closed = kubeconfig, error, False

    async def verify(self) -> list[str]:
        if self.error:
            raise ConnectorError(self.error)
        return ["default", "gc-shop", "groundcrew-system"]

    def close(self) -> None:
        self.closed = True


def _fake_kube(monkeypatch: pytest.MonkeyPatch, error: str | None = None) -> list[_FakeKubeReader]:
    from app.connectors import kubernetes as kube

    made: list[_FakeKubeReader] = []

    def factory(kubeconfig: str, **_: object) -> _FakeKubeReader:
        made.append(_FakeKubeReader(kubeconfig, error))
        return made[-1]

    monkeypatch.setattr(kube, "live_reader", factory)
    return made


async def test_k8s_is_verified_live_and_stored_encrypted(
    client: httpx.AsyncClient,
    alice: Member,
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = _fake_kube(monkeypatch)
    k8s = await client.post(
        "/connections/k8s",
        json={"workspace_id": str(alice.workspace_id), "kubeconfig": KUBECONFIG},
        headers=alice.headers,
    )
    assert k8s.status_code == 200, k8s.text
    body = k8s.json()
    assert body["server"] == "https://1.2.3.4" and "kube-token-value" not in k8s.text
    assert body["preview"] is False and body["status"] == "connected"
    assert made[0].kubeconfig == KUBECONFIG and made[0].closed
    async with session_factory() as s:
        conn = await s.get(Connection, uuid.UUID(body["id"]))
        assert conn is not None and conn.secret_encrypted
        assert "kube-token-value" not in conn.secret_encrypted
        assert decrypt(conn.secret_encrypted) == KUBECONFIG
        assert conn.config["namespaces"] == 3 and conn.config["verified_at"]


async def test_k8s_that_cannot_be_read_is_not_stored(
    client: httpx.AsyncClient,
    alice: Member,
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_kube(monkeypatch, error="The cluster at https://1.2.3.4 rejected the credentials (401)")
    k8s = await client.post(
        "/connections/k8s",
        json={"workspace_id": str(alice.workspace_id), "kubeconfig": KUBECONFIG},
        headers=alice.headers,
    )
    assert k8s.status_code == 422 and "401" in k8s.json()["detail"]
    async with session_factory() as s:
        assert not list(await s.scalars(select(Connection).where(Connection.kind == "k8s")))


# --- discovery, graph, delete --------------------------------------------------------


async def test_discover_starts_one_run_at_a_time(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    r = await client.post(f"/workspaces/{alice.workspace_id}/discover", headers=alice.headers)
    assert r.status_code == 202
    again = await client.post(f"/workspaces/{alice.workspace_id}/discover", headers=alice.headers)
    assert again.json()["id"] == r.json()["id"]
    async with session_factory() as s:
        run = await s.get(Run, uuid.UUID(r.json()["id"]))
        assert run is not None and run.agent == "discovery"
        jobs = list(await s.scalars(select(Job)))
        assert [j.kind for j in jobs] == ["discover_workspace"]


async def _discovered(sf: SessionFactory, workspace_id: uuid.UUID) -> dict[str, uuid.UUID]:
    inv = Inventory()
    await inv.load()
    async with sf() as s:
        conns = {
            "aws": Connection(
                workspace_id=workspace_id,
                kind="aws",
                name="AWS 881050425883",
                status="connected",
                config={
                    "role_arn": ROLE,
                    "external_id": "gc-x",
                    "account_id": "881050425883",
                    "region": "us-east-2",
                },
            ),
            "github": Connection(
                workspace_id=workspace_id,
                kind="github",
                name="GitHub",
                status="connected",
                config={"installation_id": 1, "repos": ["GroundsCrew-AI/checkout-demo"]},
            ),
            "vercel": Connection(
                workspace_id=workspace_id,
                kind="vercel",
                name="Vercel",
                status="connected",
                config={},
            ),
        }
        s.add_all(conns.values())
        await s.flush()
        run = await start_discovery(s, workspace_id)
        await s.commit()
    settings = _settings()
    await DiscoveryEngine(
        sf,
        settings=settings,
        llm=FakeLLM(reply(SUMMARY), settings=settings),
        connectors=inv.connectors(),
    ).run(run.id)
    return {k: c.id for k, c in conns.items()}


async def test_graph_and_resource_detail(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    await _discovered(session_factory, alice.workspace_id)
    r = await client.get(f"/workspaces/{alice.workspace_id}/graph", headers=alice.headers)
    assert r.status_code == 200
    graph = r.json()
    assert graph["summary"]["text"] == SUMMARY and graph["last_run_status"] == "succeeded"
    nodes = {(n["type"], n["name"]): n for n in graph["nodes"]}
    api = nodes[("ecs:service", "checkout-api")]
    assert api["coverage"] == "diagnosable" and "1/1 running" in api["facts"]
    assert (
        api["iac_url"]
        == "https://github.com/GroundsCrew-AI/checkout-demo/blob/HEAD/infra/aws/ecs.tf#L94"
    )
    by_id = {n["id"]: n["name"] for n in graph["nodes"]}
    kinds = {(by_id[e["from_id"]], e["kind"], by_id[e["to_id"]]) for e in graph["edges"]}
    assert ("checkout-demo", "calls", "checkout-api") in kinds
    assert all(e["evidence"] for e in graph["edges"])
    assert "iac_facts" not in r.text and "sk_live" not in r.text

    only_vercel = (
        await client.get(
            f"/workspaces/{alice.workspace_id}/graph",
            params={"provider": "vercel"},
            headers=alice.headers,
        )
    ).json()
    assert {n["provider"] for n in only_vercel["nodes"]} == {"vercel"}
    only_services = (
        await client.get(
            f"/workspaces/{alice.workspace_id}/graph",
            params=[("type", "ecs:service"), ("type", "rds:db")],
            headers=alice.headers,
        )
    ).json()
    assert {n["type"] for n in only_services["nodes"]} == {"ecs:service", "rds:db"}
    assert all(
        e["from_id"] in {n["id"] for n in only_services["nodes"]} for e in only_services["edges"]
    )

    detail = (await client.get(f"/resources/{api['id']}", headers=alice.headers)).json()
    assert detail["config"]["public_ips"] == ["3.15.10.20"]
    reads = next(e for e in detail["edges"] if e["kind"] == "reads")
    assert (
        reads["direction"] == "out"
        and reads["other"]["name"] == "checkout-orders-db"
        and reads["confidence"] == "high"
    )
    incoming = next(e for e in detail["edges"] if e["kind"] == "calls")
    assert incoming["direction"] == "in" and incoming["other"]["name"] == "checkout-demo"
    sources = {d["source"] for d in detail["recent_deploys"]}
    assert "ECS deployment" in sources and "GitHub Actions deploy (deploy.yml)" in sources
    run_deploy = next(d for d in detail["recent_deploys"] if d["source"].startswith("GitHub"))
    assert run_deploy["ref"] == "cef79e7" and run_deploy["status"] == "success"
    repo = nodes[("github:repository", "GroundsCrew-AI/checkout-demo")]
    repo_detail = await client.get(f"/resources/{repo['id']}", headers=alice.headers)
    assert "iac_facts" not in repo_detail.text


async def test_delete_removes_our_data_and_explains_cleanup(
    client: httpx.AsyncClient,
    alice: Member,
    session_factory: SessionFactory,
    sessions: FakeSessions,
) -> None:
    ids = await _discovered(session_factory, alice.workspace_id)
    r = await client.delete(f"/connections/{ids['aws']}", headers=alice.headers)
    assert r.status_code == 200
    body = r.json()
    assert body["removed_resources"] > 10
    assert any(
        "aws cloudformation delete-stack --region us-east-2 --stack-name groundcrew-read-only" in c
        for c in body["cleanup"]
    )
    assert sessions.forgotten == [ROLE]
    async with session_factory() as s:
        assert await s.get(Connection, ids["aws"]) is None
        left = list(
            await s.scalars(select(Resource).where(Resource.workspace_id == alice.workspace_id))
        )
        assert left and not any(x.provider == "aws" for x in left)
    listed = (
        await client.get(
            "/connections", params={"workspace_id": str(alice.workspace_id)}, headers=alice.headers
        )
    ).json()
    assert {c["kind"] for c in listed} == {"github", "vercel"}
    assert all(c["last_discovery"]["status"] == "succeeded" for c in listed)


async def test_refresh_needs_a_finished_connection(
    client: httpx.AsyncClient, alice: Member, sessions: FakeSessions
) -> None:
    start = (
        await client.post(
            "/connections/aws/start",
            json={"workspace_id": str(alice.workspace_id)},
            headers=alice.headers,
        )
    ).json()
    r = await client.post(f"/connections/{start['connection_id']}/discover", headers=alice.headers)
    assert r.status_code == 409


OPERATE_ROLE = "arn:aws:iam::881050425883:role/groundcrew-operate"


async def test_aws_operate_switch(
    client: httpx.AsyncClient, alice: Member, sessions: FakeSessions
) -> None:
    start = await _start(client, alice, "observe")
    cid = start["connection_id"]
    done = await client.post(
        "/connections/aws/complete",
        json={"connection_id": cid, "role_arn": ROLE},
        headers=alice.headers,
    )
    assert done.json()["capabilities"] == ["observe"] and done.json()["operate_role_arn"] is None

    sessions.missing.add(OPERATE_ROLE)  # the stack was not updated yet
    refused = await client.post(
        f"/connections/{cid}/aws/operate", json={"enabled": True}, headers=alice.headers
    )
    assert refused.status_code == 422 and "EnableOperate=true" in refused.json()["detail"]

    sessions.missing.clear()
    on = await client.post(
        f"/connections/{cid}/aws/operate", json={"enabled": True}, headers=alice.headers
    )
    assert on.status_code == 200, on.text
    assert on.json()["capabilities"] == ["observe", "operate"]
    assert on.json()["operate_role_arn"] == OPERATE_ROLE
    assert (OPERATE_ROLE, start["external_id"]) in sessions.verified

    off = await client.post(
        f"/connections/{cid}/aws/operate", json={"enabled": False}, headers=alice.headers
    )
    assert off.json()["capabilities"] == ["observe"] and OPERATE_ROLE in sessions.forgotten
