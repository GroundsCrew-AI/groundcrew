"""Kubernetes deploys to a real cluster: per-connection deploy settings (registry, push and
pull credentials, ingress class, app domain, TLS, verify URL), the settings endpoint, and the
local kind default staying exactly as it was. Everything runs on fakes: no cluster, no registry."""

import ipaddress
import json
import uuid
from typing import Any, cast

import httpx
import pytest
import yaml
from sqlalchemy import select

from app.config import Settings
from app.connectors import kubernetes as kube
from app.connectors.credentials import CredentialsProvider
from app.db.models import Connection, Workspace
from app.db.session import SessionFactory
from app.policy.actions import TARGET_APPLY
from app.security.crypto import decrypt, encrypt
from app.targets import K8sTarget, PlanMismatchError, target_for
from app.targets.k8s_deploy import K8sDeploySettings, is_local_kind
from tests.target_fakes import FakeKubeApi, FakeSandbox, approved_action, spec
from tests.test_connections_api import (  # noqa: F401 (fixtures)
    Member,
    alice,
    client,
    mallory,
    secrets_key,
)

pytestmark = pytest.mark.anyio

TOKEN = "ghp_registrytoken123"  # noqa: S105 (test value)
REAL_SERVER = "https://ABCD.gr7.us-east-1.eks.amazonaws.com"
KIND_SERVER = "https://groundcrew-control-plane:6443"


class NoCredentials:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError("the fakes replace credentials")


async def no_sleep(_seconds: float) -> None:
    return None


def _settings() -> Settings:
    return Settings(gc_kind_registry="kind-registry:5000", gc_verify_timeout_seconds=30)


def _no_dns_private(host: str) -> bool:
    """is_private_host without DNS: literal addresses and localhost only."""
    if host == "localhost":
        return True
    try:
        a = ipaddress.ip_address(host)
    except ValueError:
        return False
    return a.is_private or a.is_loopback or a.is_link_local


@pytest.fixture(autouse=True)
def no_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kube, "is_private_host", _no_dns_private)


@pytest.fixture
def overrides() -> dict[str, Any]:
    """Like production: private registries and verify URLs are refused (the test suite
    otherwise allows private servers for kind, tests/conftest.py)."""
    return {"k8s_allow_private_servers": False}


def real_settings(**overrides: Any) -> K8sDeploySettings:
    values: dict[str, Any] = {
        "registry": "ghcr.io/acme",
        "username": "acme-bot",
        "password_encrypted": encrypt(TOKEN),
        "pull_secret": True,
        "ingress_class": "alb",
        "app_domain": "apps.acme.dev",
        "tls": True,
        "tls_cluster_issuer": "letsencrypt",
    }
    values.update(overrides)
    return K8sDeploySettings(**values)


def target(
    sf: SessionFactory,
    *,
    deploy: K8sDeploySettings | None,
    server: str | None = REAL_SERVER,
    api: FakeKubeApi | None = None,
    events: list[str] | None = None,
    http: httpx.AsyncClient | None = None,
) -> tuple[K8sTarget, FakeKubeApi]:
    api = api or FakeKubeApi(events if events is not None else [])
    return (
        K8sTarget(
            _settings(),
            cast(CredentialsProvider, NoCredentials()),
            uuid.uuid4(),
            cluster="prod",
            api=api,
            http=http,
            sleep=no_sleep,
            session_factory=sf,
            deploy=deploy,
            server=server,
        ),
        api,
    )


# --- the settings endpoint --------------------------------------------------------------------


async def _cluster(sf: SessionFactory, member: Member, server: str = REAL_SERVER) -> uuid.UUID:
    async with sf() as session:
        conn = Connection(
            workspace_id=member.workspace_id,
            kind="k8s",
            name="Kubernetes prod",
            status="connected",
            config={"cluster": "prod", "server": server, "namespaces": 4},
            secret_encrypted=encrypt("apiVersion: v1\nkind: Config\n"),
        )
        session.add(conn)
        await session.commit()
        return conn.id


BODY = {
    "registry": "ghcr.io/acme",
    "username": "acme-bot",
    "token": TOKEN,
    "pull_secret": True,
    "ingress_class": "nginx",
    "app_domain": "apps.acme.dev",
    "tls": True,
    "tls_cluster_issuer": "letsencrypt",
}


async def test_owner_sets_deploy_settings_and_the_token_is_encrypted_never_returned(
    client: httpx.AsyncClient,  # noqa: F811
    alice: Member,  # noqa: F811
    session_factory: SessionFactory,
) -> None:
    conn_id = await _cluster(session_factory, alice)
    url = f"/connections/{conn_id}/k8s/deploy-settings"
    response = await client.put(url, json=BODY, headers=alice.headers)
    assert response.status_code == 200, response.text
    assert TOKEN not in response.text
    out = response.json()["deploy_settings"]
    assert out == {
        "registry": "ghcr.io/acme",
        "username": "acme-bot",
        "token_saved": True,
        "pull_secret": True,
        "ingress_class": "nginx",
        "app_domain": "apps.acme.dev",
        "tls": True,
        "tls_cluster_issuer": "letsencrypt",
        "public_url_base": None,
        "registry_auth": "token",
        "aws_connection_id": None,
    }
    async with session_factory() as session:
        stored = await session.scalar(select(Connection).where(Connection.id == conn_id))
        assert stored is not None
        raw = stored.config["deploy"]
        assert TOKEN not in json.dumps(stored.config)
        assert decrypt(raw["password_encrypted"]) == TOKEN
        assert stored.config["server"] == REAL_SERVER  # the rest of config is kept

    listed = await client.get(
        "/connections", params={"workspace_id": str(alice.workspace_id)}, headers=alice.headers
    )
    assert TOKEN not in listed.text and "password_encrypted" not in listed.text

    # Saving again without a token keeps the saved one for the same username.
    again = await client.put(
        url, json={**BODY, "token": None, "ingress_class": "traefik"}, headers=alice.headers
    )
    assert again.status_code == 200, again.text
    assert again.json()["deploy_settings"]["token_saved"] is True
    async with session_factory() as session:
        stored = await session.get_one(Connection, conn_id)
        assert decrypt(stored.config["deploy"]["password_encrypted"]) == TOKEN
        assert stored.config["deploy"]["ingress_class"] == "traefik"

    # No username clears the credentials; DELETE goes back to the kind defaults.
    cleared = await client.put(
        url,
        json={"registry": "docker.io/acme", "app_domain": "apps.acme.dev"},
        headers=alice.headers,
    )
    assert cleared.json()["deploy_settings"]["token_saved"] is False
    gone = await client.delete(url, headers=alice.headers)
    assert gone.status_code == 200 and gone.json()["deploy_settings"] is None
    async with session_factory() as session:
        stored = await session.get_one(Connection, conn_id)
        assert "deploy" not in stored.config


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"registry": "https://ghcr.io/acme"}, "without a scheme"),
        ({"registry": "ghcr.io/Acme"}, "lowercase"),
        ({"registry": "ghcr.io:99999/acme"}, "host[:port]"),
        ({"registry": "localhost:5001"}, "private or internal"),
        ({"registry": "10.0.0.5/acme"}, "private or internal"),
        ({"app_domain": "not a domain"}, "DNS name"),
        ({"app_domain": None}, "TLS needs an app domain"),
        ({"ingress_class": "Bad_Class"}, "ingress class"),
        ({"token": None}, "registry token"),
        ({"username": None}, "needs a username"),
        ({"username": None, "token": None}, "pull secret needs"),
        ({"public_url_base": "ftp://lb.acme.dev"}, "http(s) URL"),
        ({"public_url_base": "https://lb.acme.dev/path"}, "no path"),
        ({"public_url_base": "http://169.254.169.254"}, "private or internal"),
        ({"tls": False}, "issuer needs TLS"),
    ],
)
async def test_deploy_settings_are_validated(
    client: httpx.AsyncClient,  # noqa: F811
    alice: Member,  # noqa: F811
    session_factory: SessionFactory,
    change: dict[str, Any],
    message: str,
) -> None:
    conn_id = await _cluster(session_factory, alice)
    response = await client.put(
        f"/connections/{conn_id}/k8s/deploy-settings",
        json={**BODY, **change},
        headers=alice.headers,
    )
    assert response.status_code == 422, response.text
    assert message in response.json()["detail"]
    assert TOKEN not in response.text
    async with session_factory() as session:
        stored = await session.get_one(Connection, conn_id)
        assert "deploy" not in stored.config


@pytest.mark.parametrize("overrides", [{"app_env": "dev", "k8s_allow_private_servers": None}])
async def test_dev_allows_the_local_registry(
    client: httpx.AsyncClient,  # noqa: F811
    alice: Member,  # noqa: F811
    session_factory: SessionFactory,
) -> None:
    conn_id = await _cluster(session_factory, alice, KIND_SERVER)
    response = await client.put(
        f"/connections/{conn_id}/k8s/deploy-settings",
        json={"registry": "localhost:5001", "app_domain": "localhost"},
        headers=alice.headers,
    )
    assert response.status_code == 200, response.text


async def test_only_owners_of_k8s_connections_change_deploy_settings(
    client: httpx.AsyncClient,  # noqa: F811
    alice: Member,  # noqa: F811
    mallory: Member,  # noqa: F811
    session_factory: SessionFactory,
) -> None:
    conn_id = await _cluster(session_factory, alice)
    url = f"/connections/{conn_id}/k8s/deploy-settings"
    assert (await client.put(url, json=BODY, headers=mallory.headers)).status_code == 404
    assert (await client.delete(url, headers=mallory.headers)).status_code == 404
    async with session_factory() as session:
        vercel = Connection(
            workspace_id=alice.workspace_id, kind="vercel", name="v", status="connected"
        )
        session.add(vercel)
        await session.commit()
        vercel_id = vercel.id
    response = await client.put(
        f"/connections/{vercel_id}/k8s/deploy-settings", json=BODY, headers=alice.headers
    )
    assert response.status_code == 422


# --- plan, apply, verify on a real cluster ----------------------------------------------------


async def test_plan_uses_the_registry_host_ingress_class_pull_secret_and_tls(
    session_factory: SessionFactory,
) -> None:
    t, _ = target(session_factory, deploy=real_settings())
    plan = await t.plan(spec(uuid.uuid4(), uuid.uuid4()))
    assert plan.image == "ghcr.io/acme/legacy-flask-app:3f2a9c1b7d4e"
    assert plan.expected_url == "https://legacy-flask-app.apps.acme.dev"
    assert [(s.kind, s.name) for s in plan.steps] == [
        ("image", "ghcr.io/acme/legacy-flask-app:3f2a9c1b7d4e"),
        ("Namespace", "gc-legacy-flask-app"),
        ("RoleBinding", "groundcrew-deployer"),
        ("Secret", "legacy-flask-app-env"),
        ("Secret", "legacy-flask-app-registry"),
        ("Deployment", "legacy-flask-app"),
        ("Service", "legacy-flask-app"),
        ("Ingress", "legacy-flask-app"),
    ]
    assert "login as acme-bot" in (plan.steps[0].detail or "")
    assert TOKEN not in plan.model_dump_json()
    objects = {(o["kind"], o["metadata"]["name"]): o for o in yaml.safe_load_all(plan.rendered)}
    pull = objects[("Secret", "legacy-flask-app-registry")]
    assert pull["type"] == "kubernetes.io/dockerconfigjson"
    assert pull["stringData"] == {".dockerconfigjson": "********"}
    pod = objects[("Deployment", "legacy-flask-app")]["spec"]["template"]["spec"]
    assert pod["imagePullSecrets"] == [{"name": "legacy-flask-app-registry"}]
    assert pod["containers"][0]["image"] == plan.image
    ingress = objects[("Ingress", "legacy-flask-app")]
    assert ingress["spec"]["ingressClassName"] == "alb"
    assert ingress["spec"]["rules"][0]["host"] == "legacy-flask-app.apps.acme.dev"
    assert ingress["spec"]["tls"] == [
        {"hosts": ["legacy-flask-app.apps.acme.dev"], "secretName": "legacy-flask-app-tls"}
    ]
    assert ingress["metadata"]["annotations"] == {"cert-manager.io/cluster-issuer": "letsencrypt"}


async def test_plan_without_pull_secret_or_tls(session_factory: SessionFactory) -> None:
    t, _ = target(
        session_factory,
        deploy=real_settings(pull_secret=False, tls=False, tls_cluster_issuer=None),
    )
    plan = await t.plan(spec(uuid.uuid4(), uuid.uuid4()))
    assert plan.expected_url == "http://legacy-flask-app.apps.acme.dev"
    assert "imagePullSecrets" not in plan.rendered and "tls:" not in plan.rendered
    assert "annotations: {cert-manager" not in plan.rendered


async def test_apply_pushes_with_the_login_and_writes_the_pull_secret(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    events: list[str] = []
    t, api = target(session_factory, deploy=real_settings(), events=events)
    sandbox = FakeSandbox(events)
    run, _, _ = await approved_action(
        session_factory, workspace, action=TARGET_APPLY, plan_hash="0" * 64
    )
    the_spec = spec(run.id, t.connection_id)
    plan = await t.plan(the_spec)
    _, _, token = await approved_action(
        session_factory, workspace, action=TARGET_APPLY, plan_hash=plan.plan_hash(), run=run
    )
    assert token is not None
    deployment = await t.apply(plan, the_spec, token, sandbox)

    assert events[0] == (
        "push gc-build/legacy-flask-app:3f2a9c1b7d4e -> ghcr.io/acme/legacy-flask-app:3f2a9c1b7d4e"
    )
    assert events.index("apply secrets/legacy-flask-app-registry") < events.index(
        "apply deployments/legacy-flask-app"
    )
    ((_, push),) = sandbox.pushed
    assert push.login is not None
    assert push.login.registry == "ghcr.io" and push.login.username == "acme-bot"
    assert push.login.password.get_secret_value() == TOKEN
    pull = api.objects[("secrets", "gc-legacy-flask-app", "legacy-flask-app-registry")]
    config = json.loads(pull["stringData"][".dockerconfigjson"])
    assert config["auths"]["ghcr.io"]["username"] == "acme-bot"
    assert config["auths"]["ghcr.io"]["password"] == TOKEN
    assert deployment.live_url == "https://legacy-flask-app.apps.acme.dev"


def test_docker_hub_pull_secret_uses_the_index_key() -> None:
    settings = real_settings(registry="docker.io/acme")
    assert list(json.loads(settings.dockerconfigjson())["auths"]) == ["https://index.docker.io/v1/"]
    assert settings.push_target("app", "t1").image == "docker.io/acme/app:t1"


async def test_push_without_credentials_has_no_login(session_factory: SessionFactory) -> None:
    deploy = real_settings(username=None, password_encrypted=None, pull_secret=False)
    assert deploy.push_target("app", "t1").login is None
    t, _ = target(session_factory, deploy=deploy)
    plan = await t.plan(spec(uuid.uuid4(), uuid.uuid4()))
    assert "login as" not in (plan.steps[0].detail or "")
    assert "imagePullSecrets" not in plan.rendered


async def test_apply_refuses_when_the_settings_changed_after_the_plan(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    events: list[str] = []
    t, _ = target(session_factory, deploy=real_settings(), events=events)
    run, _, _ = await approved_action(
        session_factory, workspace, action=TARGET_APPLY, plan_hash="0" * 64
    )
    the_spec = spec(run.id, t.connection_id)
    plan = await t.plan(the_spec)
    _, _, token = await approved_action(
        session_factory, workspace, action=TARGET_APPLY, plan_hash=plan.plan_hash(), run=run
    )
    assert token is not None
    t.deploy = real_settings(ingress_class="nginx")
    with pytest.raises(PlanMismatchError, match="deploy settings changed"):
        await t.apply(plan, the_spec, token, FakeSandbox(events))
    assert events == []  # nothing pushed, nothing written


async def test_verify_uses_the_app_url_or_the_public_url_base(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    for deploy, want_url in (
        (real_settings(), "https://legacy-flask-app.apps.acme.dev/health"),
        (
            real_settings(public_url_base="https://lb.acme.dev"),
            "https://lb.acme.dev/health",
        ),
    ):
        seen: list[httpx.Request] = []

        def handle(request: httpx.Request, seen: list[httpx.Request] = seen) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200)

        http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        t, _ = target(session_factory, deploy=deploy, http=http)
        run, _, _ = await approved_action(
            session_factory, workspace, action=TARGET_APPLY, plan_hash="0" * 64
        )
        the_spec = spec(run.id, t.connection_id)
        plan = await t.plan(the_spec)
        _, _, token = await approved_action(
            session_factory, workspace, action=TARGET_APPLY, plan_hash=plan.plan_hash(), run=run
        )
        assert token is not None
        deployment = await t.apply(plan, the_spec, token, FakeSandbox([]))
        health = await t.verify(deployment)
        assert health.ok, health
        assert str(seen[0].url) == want_url
        assert seen[0].headers["host"] == "legacy-flask-app.apps.acme.dev"
        assert health.checks[1].url == "https://legacy-flask-app.apps.acme.dev/health"


# --- preflight and the kind default -----------------------------------------------------------


async def test_preflight_says_a_real_cluster_needs_deploy_settings(
    session_factory: SessionFactory,
) -> None:
    t, _ = target(session_factory, deploy=None, server=REAL_SERVER)
    checks = await t.preflight(await t.plan(spec(uuid.uuid4(), uuid.uuid4())))
    registry = next(c for c in checks if c.name == "other")
    assert registry.status == "fail"
    assert "not the local kind cluster" in registry.detail
    assert "Deploy settings on the Connections page" in registry.detail
    assert REAL_SERVER in registry.detail

    t, _ = target(session_factory, deploy=real_settings())
    checks = await t.preflight(await t.plan(spec(uuid.uuid4(), uuid.uuid4())))
    registry = next(c for c in checks if c.name == "other")
    assert registry.status == "pass"
    assert "ghcr.io/acme (login as acme-bot)" in registry.detail

    t, _ = target(
        session_factory, deploy=real_settings(app_domain=None, tls=False, tls_cluster_issuer=None)
    )
    checks = await t.preflight(await t.plan(spec(uuid.uuid4(), uuid.uuid4())))
    assert next(c for c in checks if c.name == "other").status == "warn"


@pytest.mark.parametrize(
    ("server", "local"),
    [
        (None, True),
        (KIND_SERVER, True),
        ("https://127.0.0.1:40123", True),
        ("https://localhost:6443", True),
        (REAL_SERVER, False),
        ("https://10.0.0.1:6443", False),
    ],
)
def test_is_local_kind(server: str | None, local: bool) -> None:
    assert is_local_kind(server) is local


async def test_kind_default_is_unchanged(session_factory: SessionFactory) -> None:
    """A k8s connection without deploy settings deploys exactly as before."""
    conn = Connection(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        kind="k8s",
        name="kind",
        status="connected",
        config={"cluster": "kind-groundcrew", "server": KIND_SERVER},
    )
    t = target_for(_settings(), cast(CredentialsProvider, NoCredentials()), conn)
    assert isinstance(t, K8sTarget) and t.deploy is None and t.server == KIND_SERVER
    t._api = FakeKubeApi([])
    plan = await t.plan(spec(uuid.uuid4(), conn.id))
    assert plan.image == "kind-registry:5000/legacy-flask-app:3f2a9c1b7d4e"
    assert plan.expected_url == "http://legacy-flask-app.localhost"
    assert "imagePullSecrets" not in plan.rendered and "tls:" not in plan.rendered
    assert "ingressClassName: nginx" in plan.rendered
    assert "host: legacy-flask-app.localhost" in plan.rendered
    assert t._push_target("legacy-flask-app", "t1").login is None
    checks = await t.preflight(plan)
    assert next(c for c in checks if c.name == "other").status == "pass"

    conn.config = {**conn.config, "deploy": real_settings().to_config()}
    real = target_for(_settings(), cast(CredentialsProvider, NoCredentials()), conn)
    assert isinstance(real, K8sTarget) and real.deploy == real_settings(
        password_encrypted=real.deploy.password_encrypted if real.deploy else None
    )
