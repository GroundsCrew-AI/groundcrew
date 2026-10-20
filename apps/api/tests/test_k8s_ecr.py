"""Kubernetes deploys that push to Amazon ECR through an AWS connection (registry_auth "ecr"):
the settings endpoint and its checks, a fresh login at every push, the groundcrew-<app>
repository created when missing, the pull secret rewritten with a fresh token, and preflight
messages. Everything runs on fakes: no AWS, no cluster, no registry."""

import base64
import json
import uuid
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import select

from app.connectors.credentials import CredentialsError, CredentialsProvider
from app.db.models import Connection, Workspace
from app.db.session import SessionFactory
from app.policy.actions import TARGET_APPLY
from app.sandbox.registry import ecr_login
from app.targets import K8sTarget
from app.targets.k8s_deploy import K8sDeploySettings
from tests.target_fakes import (
    ACCOUNT,
    FakeAws,
    FakeKubeApi,
    FakeSandbox,
    approved_action,
    client_error,
    spec,
)
from tests.test_connections_api import (  # noqa: F401 (fixtures)
    Member,
    alice,
    client,
    mallory,
    secrets_key,
)
from tests.test_k8s_deploy_settings import (  # noqa: F401 (fixtures)
    REAL_SERVER,
    _cluster,
    _settings,
    no_dns,
    no_sleep,
    overrides,
)

pytestmark = pytest.mark.anyio

REGION = "us-east-2"
REGISTRY = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"
REPOSITORY = "groundcrew-legacy-flask-app"


class RegionAws(FakeAws):
    """FakeAws that records the region of each client and can hand out a new token."""

    def __init__(self, events: list[str]) -> None:
        super().__init__(events)
        self.regions: list[str | None] = []
        self.password = "ecr-password-1"  # noqa: S105 (test value)
        self.refuse: str | None = None

    def client(self, name: str, region_name: str | None = None) -> Any:
        self.regions.append(region_name)
        return super().client(name, region_name)

    def ecr__describe_repositories(self, repositoryNames: list[str]) -> dict[str, Any]:  # noqa: N803
        if self.refuse:
            raise client_error(self.refuse, "DescribeRepositories", "not authorized")
        return super().ecr__describe_repositories(repositoryNames)

    def ecr__get_authorization_token(self) -> dict[str, Any]:
        token = base64.b64encode(f"AWS:{self.password}".encode()).decode()
        endpoint = f"https://{REGISTRY}"
        return {"authorizationData": [{"authorizationToken": token, "proxyEndpoint": endpoint}]}


class AwsCredentials:
    """Only the deploy session of one AWS connection."""

    def __init__(self, aws: FakeAws, connection_id: uuid.UUID, error: str | None = None) -> None:
        self.aws, self.connection_id, self.error = aws, connection_id, error
        self.sessions = 0

    def aws_deploy_session(self, connection_id: uuid.UUID) -> Any:
        assert connection_id == self.connection_id
        if self.error:
            raise CredentialsError(self.error)
        self.sessions += 1
        return self.aws

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"unexpected credentials call {name}")


def ecr_settings(aws_id: uuid.UUID, **changes: Any) -> K8sDeploySettings:
    values: dict[str, Any] = {
        "registry": REGISTRY,
        "registry_auth": "ecr",
        "aws_connection_id": str(aws_id),
        "pull_secret": True,
        "app_domain": "apps.acme.dev",
    }
    values.update(changes)
    return K8sDeploySettings(**values)


def ecr_target(
    sf: SessionFactory,
    aws: FakeAws,
    *,
    error: str | None = None,
    api: FakeKubeApi | None = None,
    **changes: Any,
) -> tuple[K8sTarget, FakeKubeApi, AwsCredentials]:
    aws_id = uuid.uuid4()
    credentials = AwsCredentials(aws, aws_id, error)
    api = api or FakeKubeApi(aws.events)
    target = K8sTarget(
        _settings(),
        cast(CredentialsProvider, credentials),
        uuid.uuid4(),
        cluster="prod",
        api=api,
        sleep=no_sleep,
        session_factory=sf,
        deploy=ecr_settings(aws_id, **changes),
        server=REAL_SERVER,
    )
    return target, api, credentials


# --- the settings endpoint --------------------------------------------------------------------


async def _aws(
    sf: SessionFactory, member: Member, *, tier: str = "deploy", account: str = ACCOUNT
) -> uuid.UUID:
    async with sf() as session:
        conn = Connection(
            workspace_id=member.workspace_id,
            kind="aws",
            name=f"AWS {account}",
            status="connected",
            config={
                "account_id": account,
                "external_id": "ext-1",
                "role_arn": f"arn:aws:iam::{account}:role/groundcrew-read-only",
                "region": REGION,
                "tier": tier,
                "deploy_role_arn": (
                    f"arn:aws:iam::{account}:role/groundcrew-deploy" if tier == "deploy" else None
                ),
            },
        )
        session.add(conn)
        await session.commit()
        return conn.id


def _body(aws_id: uuid.UUID | str | None, **change: Any) -> dict[str, Any]:
    return {
        "registry": REGISTRY,
        "registry_auth": "ecr",
        "aws_connection_id": str(aws_id) if aws_id else None,
        "pull_secret": True,
        "app_domain": "apps.acme.dev",
        **change,
    }


async def test_owner_chooses_ecr_through_an_aws_connection(
    client: httpx.AsyncClient,  # noqa: F811
    alice: Member,  # noqa: F811
    session_factory: SessionFactory,
) -> None:
    conn_id = await _cluster(session_factory, alice)
    aws_id = await _aws(session_factory, alice)
    url = f"/connections/{conn_id}/k8s/deploy-settings"
    response = await client.put(url, json=_body(aws_id), headers=alice.headers)
    assert response.status_code == 200, response.text
    out = response.json()["deploy_settings"]
    assert out["registry_auth"] == "ecr" and out["aws_connection_id"] == str(aws_id)
    assert out["registry"] == REGISTRY and out["pull_secret"] is True
    assert out["username"] is None and out["token_saved"] is False
    async with session_factory() as session:
        stored = await session.scalar(select(Connection).where(Connection.id == conn_id))
        assert stored is not None
        assert stored.config["deploy"]["password_encrypted"] is None

    # Back to a token registry: the AWS connection is forgotten.
    token_body = {"registry": "ghcr.io/acme", "app_domain": "apps.acme.dev"}
    again = await client.put(url, json=token_body, headers=alice.headers)
    assert again.json()["deploy_settings"]["registry_auth"] == "token"
    assert again.json()["deploy_settings"]["aws_connection_id"] is None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"registry": f"{REGISTRY}/team"}, "without a path"),
        ({"registry": "ghcr.io/acme"}, "without a path"),
        ({"registry": f"{ACCOUNT}.dkr.ecr.eu-west-1.amazonaws.com"}, "not eu-west-1"),
        ({"registry": f"111122223333.dkr.ecr.{REGION}.amazonaws.com"}, "account 111122223333"),
        ({"username": "AWS", "token": "x"}, "leave the username and token empty"),
        ({"aws_connection_id": None}, "this workspace"),
        ({"aws_connection_id": str(uuid.uuid4())}, "this workspace"),
    ],
)
async def test_ecr_settings_are_validated(
    client: httpx.AsyncClient,  # noqa: F811
    alice: Member,  # noqa: F811
    session_factory: SessionFactory,
    change: dict[str, Any],
    message: str,
) -> None:
    conn_id = await _cluster(session_factory, alice)
    aws_id = await _aws(session_factory, alice)
    response = await client.put(
        f"/connections/{conn_id}/k8s/deploy-settings",
        json={**_body(aws_id), **change},
        headers=alice.headers,
    )
    assert response.status_code == 422, response.text
    assert message in response.json()["detail"]


async def test_ecr_needs_a_deploy_capable_aws_connection_of_the_same_workspace(
    client: httpx.AsyncClient,  # noqa: F811
    alice: Member,  # noqa: F811
    mallory: Member,  # noqa: F811
    session_factory: SessionFactory,
) -> None:
    conn_id = await _cluster(session_factory, alice)
    url = f"/connections/{conn_id}/k8s/deploy-settings"
    observe = await _aws(session_factory, alice, tier="observe", account="222233334444")
    response = await client.put(
        url,
        json=_body(observe, registry=f"222233334444.dkr.ecr.{REGION}.amazonaws.com"),
        headers=alice.headers,
    )
    assert response.status_code == 422 and "cannot deploy" in response.json()["detail"]

    other = await _aws(session_factory, mallory)
    response = await client.put(url, json=_body(other), headers=alice.headers)
    assert response.status_code == 422 and "this workspace" in response.json()["detail"]

    response = await client.put(url, json=_body(conn_id), headers=alice.headers)
    assert response.status_code == 422 and "not an AWS connection" in response.json()["detail"]
    async with session_factory() as session:
        stored = await session.get_one(Connection, conn_id)
        assert "deploy" not in stored.config


# --- plan, preflight, apply -------------------------------------------------------------------


async def test_plan_names_the_groundcrew_repository_and_the_ecr_login(
    session_factory: SessionFactory,
) -> None:
    t, _, credentials = ecr_target(session_factory, RegionAws([]))
    plan = await t.plan(spec(uuid.uuid4(), uuid.uuid4()))
    assert plan.image == f"{REGISTRY}/{REPOSITORY}:3f2a9c1b7d4e"
    assert "ECR login through the AWS connection" in (plan.steps[0].detail or "")
    assert f"{REPOSITORY} created if missing" in (plan.steps[0].detail or "")
    assert ("Secret", "legacy-flask-app-registry") in [(s.kind, s.name) for s in plan.steps]
    assert credentials.sessions == 0  # planning makes no AWS call


async def test_preflight_checks_the_repository_read_only(session_factory: SessionFactory) -> None:
    aws = RegionAws([])
    t, _, _ = ecr_target(session_factory, aws)
    plan = await t.plan(spec(uuid.uuid4(), uuid.uuid4()))
    details = [c.detail for c in await t.preflight(plan) if c.name == "other"]
    assert any("Amazon ECR, login through the AWS connection" in d for d in details)
    assert any(f"{REPOSITORY} is created by apply" in d for d in details)
    assert aws.ops() == ["ecr:describe_repositories"] and aws.regions == [REGION]

    aws.repositories.add(REPOSITORY)
    details = [c.detail for c in await t.preflight(plan) if c.name == "other"]
    assert any(f"ECR repository {REPOSITORY} exists" in d for d in details)

    aws.refuse = "AccessDeniedException"
    check = [c for c in await t.preflight(plan) if c.name == "other"][-1]
    assert check.status == "fail"
    assert "groundcrew-deploy role may only use ECR repositories named groundcrew-*" in (
        check.detail
    )

    t, _, _ = ecr_target(session_factory, RegionAws([]), error="AWS 1 has no verified deploy role")
    check = [c for c in await t.preflight(plan) if c.name == "other"][-1]
    assert check.status == "fail" and "no verified deploy role" in check.detail


async def _apply(
    sf: SessionFactory, workspace: Workspace, t: K8sTarget, sandbox: FakeSandbox
) -> None:
    run, _, _ = await approved_action(sf, workspace, action=TARGET_APPLY, plan_hash="0" * 64)
    the_spec = spec(run.id, t.connection_id)
    plan = await t.plan(the_spec)
    _, _, token = await approved_action(
        sf, workspace, action=TARGET_APPLY, plan_hash=plan.plan_hash(), run=run
    )
    assert token is not None
    await t.apply(plan, the_spec, token, sandbox)


async def test_apply_creates_the_repository_and_logs_in_fresh_at_every_push(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    events: list[str] = []
    aws = RegionAws(events)
    t, api, credentials = ecr_target(session_factory, aws)
    sandbox = FakeSandbox(events)
    await _apply(session_factory, workspace, t, sandbox)

    image = f"{REGISTRY}/{REPOSITORY}:3f2a9c1b7d4e"
    assert events[:2] == [
        f"create ecr {REPOSITORY}",
        f"push gc-build/legacy-flask-app:3f2a9c1b7d4e -> {image}",
    ]
    create = next(kw for s, o, kw in aws.calls if o == "create_repository")
    assert {"Key": "project", "Value": "groundcrew"} in create["tags"]
    assert set(aws.regions) == {REGION}
    ((_, push),) = sandbox.pushed
    assert push.login is not None and push.login.registry == REGISTRY
    assert push.login.password.get_secret_value() == "ecr-password-1"
    key = ("secrets", "gc-legacy-flask-app", "legacy-flask-app-registry")
    config = json.loads(api.objects[key]["stringData"][".dockerconfigjson"])
    assert config["auths"][REGISTRY]["password"] == "ecr-password-1"  # noqa: S105
    assert "ecr-password-1" not in json.dumps(t.deploy.to_config() if t.deploy else {})

    # The next deploy: the repository exists, the token is new, and so is the pull secret.
    aws.password = "ecr-password-2"  # noqa: S105 (test value)
    await _apply(session_factory, workspace, t, FakeSandbox(events))
    assert [o for _, o, _ in aws.calls].count("create_repository") == 1
    assert [o for _, o, _ in aws.calls].count("get_authorization_token") == 2
    config = json.loads(api.objects[key]["stringData"][".dockerconfigjson"])
    assert config["auths"][REGISTRY]["password"] == "ecr-password-2"  # noqa: S105
    assert credentials.sessions >= 2


async def test_apply_without_a_pull_secret_writes_none(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    aws = RegionAws([])
    aws.repositories.add(REPOSITORY)
    t, api, _ = ecr_target(session_factory, aws, pull_secret=False)
    await _apply(session_factory, workspace, t, FakeSandbox([]))
    assert not any(name == "legacy-flask-app-registry" for _, _, name in api.objects)
    assert "create_repository" not in [o for _, o, _ in aws.calls]


async def test_a_refused_repository_create_stops_before_the_push(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    events: list[str] = []
    aws = RegionAws(events)
    aws.refuse = "AccessDeniedException"
    t, _, _ = ecr_target(session_factory, aws)
    with pytest.raises(Exception, match="groundcrew-deploy role may only use ECR"):
        await _apply(session_factory, workspace, t, FakeSandbox(events))
    assert not any(e.startswith("push") for e in events)


async def test_rollback_refreshes_the_pull_secret(session_factory: SessionFactory) -> None:
    aws = RegionAws([])
    t, api, _ = ecr_target(session_factory, aws)
    plan = await t.plan(spec(uuid.uuid4(), uuid.uuid4()))
    aws.password = "ecr-password-9"  # noqa: S105 (test value)
    await t._refresh_ecr_pull_secret(plan)
    key = ("secrets", "gc-legacy-flask-app", "legacy-flask-app-registry")
    config = json.loads(api.objects[key]["stringData"][".dockerconfigjson"])
    assert config["auths"][REGISTRY]["password"] == "ecr-password-9"  # noqa: S105

    # Best effort: a login that cannot be fetched does not stop the rollback.
    t, api, _ = ecr_target(session_factory, aws, error="gone")
    await t._refresh_ecr_pull_secret(plan)
    assert key not in api.objects


async def test_ecr_login_uses_the_registry_region() -> None:
    aws = RegionAws([])
    login = await ecr_login(cast(Any, aws), region="eu-central-1")
    assert aws.regions == ["eu-central-1"] and login.registry == REGISTRY
