"""Dev-only paths are refused in production (APP_ENV=production), each at the place it is used,
and a production instance refuses to start with any dev-only switch set."""

import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings, get_settings
from app.connectors.credentials import CredentialsProvider
from app.connectors.kubernetes import UnsafeKubeconfigError, check_kubeconfig
from app.db.models import Approval, Run, RunStatus, ToolPolicy
from app.db.session import SessionFactory, get_session_factory
from app.devtools import NotDevError, require_dev
from app.main import app
from app.pipeline.runner import default_deps
from app.routes.connections import _allow_private
from app.targets.k8s import K8sTarget

from .auth_helpers import service_headers
from .test_api import Member, _member, _settings

pytestmark = pytest.mark.anyio

SECRET = "x" * 40


def _prod(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "production",
        "database_url": SecretStr("postgresql+asyncpg://u:p@db/gc"),
        "secrets_key": SecretStr(SECRET),
        "internal_jwt_secret": SecretStr("test-internal-jwt-secret-0123456789abcdef"),
        "approval_token_key": SecretStr(SECRET),
        "web_origin": "https://groundkrew.com",
        "agent_api_url": "https://api.groundkrew.com",
        "smtp_host": "smtp.resend.com",
        # conftest turns it on for the fake API servers on 127.0.0.1.
        "k8s_allow_private_servers": None,
        **overrides,
    }
    return _settings(**values)


@pytest.fixture
async def client(session_factory: SessionFactory) -> AsyncIterator[httpx.AsyncClient]:
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = lambda: _prod()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def alice(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 7001, "alice")


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("live_script_self_approve", True),
        ("pipeline_fake", True),
        ("k8s_allow_private_servers", True),
        ("deploy_fixtures_dir", "/app/fixtures"),
    ],
)
def test_production_refuses_to_start_with_a_dev_switch(setting: str, value: Any) -> None:
    assert _prod().production_problems() == []
    problems = _prod(**{setting: value}).production_problems()
    assert problems == [f"{setting.upper()} is a local development setting"]
    with pytest.raises(RuntimeError, match="APP_ENV=production cannot start"):
        _prod(**{setting: value}).require_production_ready()


def test_kind_register_and_other_dev_tools_refuse() -> None:
    with pytest.raises(NotDevError, match="APP_ENV is 'production'"):
        require_dev(_prod(), "http://localhost:8000", tool="kind-register")


async def test_self_approve_is_not_found(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    app.dependency_overrides[get_settings] = lambda: _prod(live_script_self_approve=True)
    async with session_factory() as session:
        run = Run(
            workspace_id=alice.workspace_id,
            agent="deploy",
            goal="g",
            status=RunStatus.waiting_approval,
        )
        session.add(run)
        await session.flush()
        approval = Approval(
            run_id=run.id,
            workspace_id=alice.workspace_id,
            tool_call_id="deploy-plan",
            tool_name="target_apply",
            policy=ToolPolicy.write,
            arguments={"plan_hash": "a" * 64},
        )
        session.add(approval)
        await session.commit()
    r = await client.post(
        f"/approvals/{approval.id}/self-approve", json={"script": "live"}, headers=alice.headers
    )
    assert r.status_code == 404


async def test_dev_evidence_endpoint_is_not_found(client: httpx.AsyncClient, alice: Member) -> None:
    r = await client.get(
        f"/workspaces/{alice.workspace_id}/evidence",
        params={"resource": str(uuid.uuid4())},
        headers=alice.headers,
    )
    assert r.status_code == 404


async def test_deploy_fixtures_are_refused(client: httpx.AsyncClient, alice: Member) -> None:
    r = await client.post(
        "/deploys",
        json={
            "workspace_id": str(alice.workspace_id),
            "repo": {"fixture": "py-build-trap"},
            "target_connection_id": str(uuid.uuid4()),
        },
        headers=alice.headers,
    )
    assert r.status_code == 409
    assert "local dev only" in r.json()["detail"]


def _k8s(settings: Settings, server: str | None) -> K8sTarget:
    # The registry check reads only settings, deploy settings, and the server address.
    no_credentials = cast(CredentialsProvider, None)
    return K8sTarget(
        settings, no_credentials, uuid.uuid4(), cluster="kind", deploy=None, server=server
    )


async def test_local_kind_target_is_refused() -> None:
    for server in (None, "https://127.0.0.1:6443", "https://groundcrew-control-plane:6443"):
        check = _k8s(_prod(), server)._registry_check()
        assert check.status == "fail", server
        assert "deploy settings" in check.detail
    # Local dev keeps deploying to kind.
    dev = _k8s(_settings(app_env="dev"), None)
    assert dev._registry_check().status == "pass"


def test_private_cluster_addresses_are_refused() -> None:
    assert _allow_private(_prod()) is False
    kubeconfig = {
        "clusters": [{"name": "c", "cluster": {"server": "https://127.0.0.1:6443"}}],
        "users": [{"name": "u", "user": {"token": "t"}}],
        "contexts": [{"name": "x", "context": {"cluster": "c", "user": "u"}}],
        "current-context": "x",
    }
    with pytest.raises(UnsafeKubeconfigError):
        check_kubeconfig(kubeconfig, allow_private=_allow_private(_prod()))


async def test_pipeline_fake_is_ignored(session_factory: SessionFactory) -> None:
    deps = default_deps(session_factory, _prod(pipeline_fake=True))
    assert type(deps.sandbox).__module__ != "app.pipeline.fake"
    assert type(deps.llm).__name__ == "TokenFactory"


async def test_password_and_google_sign_in_are_off(client: httpx.AsyncClient) -> None:
    headers = service_headers()
    options = (await client.get("/auth/options", headers=headers)).json()
    assert options["password"] is False and options["google"] is False
    body = {"email": "ada@example.com", "password": "correct horse battery"}
    for path in ("sign-in", "sign-up", "forgot-password", "resend-verification"):
        r = await client.post(f"/auth/{path}", json=body, headers=headers)
        assert r.status_code == 403, path
        assert r.json()["code"] == "disabled"
    for path in ("verify-email", "reset-password"):
        r = await client.post(
            f"/auth/{path}", json={"token": "t" * 20, "password": "x" * 12}, headers=headers
        )
        assert r.status_code == 403, path
    google = {"provider": "google", "account_id": "123", "email": "ada@example.com"}
    r = await client.post("/auth/oauth", json=google, headers=headers)
    assert r.status_code == 403 and r.json()["code"] == "disabled"
    github = {"provider": "github", "account_id": "123", "login": "ada"}
    assert (await client.post("/auth/oauth", json=github, headers=headers)).status_code == 200


async def test_password_and_google_can_be_turned_back_on(client: httpx.AsyncClient) -> None:
    app.dependency_overrides[get_settings] = lambda: _prod(
        password_auth_enabled=True, google_auth_enabled=True, smtp_host="smtp.example.com"
    )
    options = (await client.get("/auth/options", headers=service_headers())).json()
    assert options["password"] is True and options["google"] is True
