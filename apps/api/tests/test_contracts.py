"""The Phase 3 contracts other phases call: CredentialsProvider and ResourceSink."""

import stat
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import boto3
import pytest
from sqlalchemy import select

from app.config import Settings, get_settings
from app.connectors.credentials import (
    ConnectionCredentialsProvider,
    CredentialsError,
    CredentialsProvider,
    EnvCredentialsProvider,
)
from app.db.models import Connection, Edge, Resource, Workspace
from app.db.session import SessionFactory
from app.discovery.engine import DiscoveryEngine
from app.discovery.types import DiscoveredResource
from app.graph.sink import (
    DEPLOYER_EXTERNAL_ID,
    SINK_RULE,
    Deployment,
    GraphResourceSink,
    NoopResourceSink,
    ResourceSink,
)
from app.security.crypto import encrypt, generate_key

pytestmark = pytest.mark.anyio

KUBECONFIG = "apiVersion: v1\nkind: Config\nclusters: []\n"


@pytest.fixture(autouse=True)
def secrets_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SECRETS_KEY", generate_key())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeSessions:
    def __init__(self) -> None:
        self.assumed: list[tuple[str, str]] = []
        self.kwargs: list[dict[str, Any]] = []
        self.settings = Settings(_env_file=None, gc_aws_region="us-east-2")

    def assume(self, role_arn: str, external_id: str, **kwargs: Any) -> boto3.Session:
        self.assumed.append((role_arn, external_id))
        self.kwargs.append(kwargs)
        return boto3.Session(region_name="us-east-2")


def _conn(kind: str, config: dict[str, Any], secret: str | None = None) -> Connection:
    return Connection(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        kind=kind,
        name=f"{kind} test",
        status="connected",
        config=config,
        secret_encrypted=encrypt(secret) if secret else None,
    )


def test_aws_session_assumes_the_role_with_its_external_id() -> None:
    aws = _conn(
        "aws", {"role_arn": "arn:aws:iam::881050425883:role/groundcrew-x", "external_id": "gc-1"}
    )
    fake = FakeSessions()
    provider: CredentialsProvider = ConnectionCredentialsProvider([aws], sessions=fake)  # type: ignore[arg-type]
    assert isinstance(provider.aws_session(aws.id), boto3.Session)
    assert fake.assumed == [("arn:aws:iam::881050425883:role/groundcrew-x", "gc-1")]


def test_aws_session_refuses_unfinished_or_wrong_connections() -> None:
    pending = _conn("aws", {"external_id": "gc-1"})
    vercel = _conn("vercel", {}, "tok")
    provider = ConnectionCredentialsProvider([pending, vercel], sessions=FakeSessions())  # type: ignore[arg-type]
    with pytest.raises(CredentialsError, match="not finished"):
        provider.aws_session(pending.id)
    with pytest.raises(CredentialsError, match="is vercel, not aws"):
        provider.aws_session(vercel.id)
    with pytest.raises(CredentialsError, match="not in this workspace"):
        provider.aws_session(uuid.uuid4())


def test_kubeconfig_is_a_private_temp_file_removed_on_close() -> None:
    k8s = _conn("k8s", {"cluster": "c"}, KUBECONFIG)
    with ConnectionCredentialsProvider([k8s]) as provider:
        path = provider.kubeconfig(k8s.id)
        assert path.read_text() == KUBECONFIG
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert provider.kubeconfig(k8s.id) == path
    assert not path.exists() and not path.parent.exists()


def test_azure_credential_keeps_the_secret_out_of_repr() -> None:
    azure = _conn("azure", {"tenant_id": "t", "client_id": "c"}, "very-secret-value")
    credential = ConnectionCredentialsProvider([azure]).azure_credential(azure.id)
    assert "very-secret-value" not in repr(credential) and "very-secret-value" not in str(
        vars(credential)
    )
    with pytest.raises(CredentialsError, match="preview"):
        credential.get_token("https://management.azure.com/.default")


def test_env_provider_reads_env_and_says_what_is_missing(tmp_path: Any) -> None:
    fake = FakeSessions()
    env = {
        "KUBECONFIG": str(tmp_path / "config"),
        "GROUNDCREW_DEV_AWS_ROLE_ARN": "arn:aws:iam::1:role/groundcrew-dev",
        "GROUNDCREW_DEV_AWS_EXTERNAL_ID": "gc-dev",
    }
    provider: CredentialsProvider = EnvCredentialsProvider(env, sessions=fake)  # type: ignore[arg-type]
    assert provider.kubeconfig(uuid.uuid4()) == tmp_path / "config"
    provider.aws_session(uuid.uuid4())
    assert fake.assumed == [("arn:aws:iam::1:role/groundcrew-dev", "gc-dev")]
    with pytest.raises(CredentialsError, match="AZURE_TENANT_ID"):
        provider.azure_credential(uuid.uuid4())


# ------------------------------------------------------------------------- ResourceSink


def _service(name: str = "checkout-api") -> DiscoveredResource:
    return DiscoveredResource(
        provider="aws",
        type="ecs:service",
        external_id=f"arn:aws:ecs:us-east-2:881050425883:service/checkout/{name}",
        name=name,
        account="881050425883",
        region="us-east-2",
        config={"desired_count": 1},
    )


async def _edges(sf: SessionFactory, ws: Workspace) -> list[tuple[str, str, str, dict[str, Any]]]:
    async with sf() as s:
        names = {r.id: r.name for r in await s.scalars(select(Resource))}
        rows = await s.scalars(select(Edge).where(Edge.workspace_id == ws.id))
        return [(names[e.from_id], e.kind, names[e.to_id], e.evidence) for e in rows]


async def test_register_deployment_upserts_resource_and_deployed_by_edge(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    sink: ResourceSink = GraphResourceSink(session_factory)
    run_id = uuid.uuid4()
    deployment = Deployment(
        resource=_service(),
        run_id=run_id,
        repo="GroundsCrew-AI/checkout-demo",
        commit="cef79e7a1b2",
    )
    first = await sink.register_deployment(workspace.id, deployment)
    again = await sink.register_deployment(workspace.id, deployment)
    assert first == again  # same row, same id
    edges = await _edges(session_factory, workspace)
    assert len(edges) == 1
    source, kind, target, evidence = edges[0]
    assert (source, kind, target) == ("checkout-api", "deployed_by", "Groundcrew")
    assert evidence["rule"] == SINK_RULE and evidence["run_id"] == str(run_id)
    assert "from GroundsCrew-AI/checkout-demo at cef79e7" in evidence["text"]
    async with session_factory() as s:
        row = await s.get(Resource, first)
        assert row is not None and row.config["last_deployment"]["commit"] == "cef79e7a1b2"
        assert row.config["desired_count"] == 1
        deployer = await s.scalar(
            select(Resource).where(Resource.external_id == DEPLOYER_EXTERNAL_ID)
        )
        assert deployer is not None and deployer.connection_id is None


async def test_registered_edges_survive_rediscovery(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    await GraphResourceSink(session_factory).register_deployment(
        workspace.id, Deployment(resource=_service())
    )
    engine = DiscoveryEngine(session_factory, connectors={})
    async with session_factory() as s:
        await engine._rebuild_graph(s, workspace.id)
    assert [(a, k, b) for a, k, b, _ in await _edges(session_factory, workspace)] == [
        ("checkout-api", "deployed_by", "Groundcrew")
    ]


async def test_noop_sink() -> None:
    sink: ResourceSink = NoopResourceSink()
    assert await sink.register_deployment(
        uuid.uuid4(), Deployment(resource=_service())
    ) == uuid.UUID(int=0)


# --- aws_deploy_session (Tier=deploy) ------------------------------------------------------

READ_ROLE = "arn:aws:iam::881050425883:role/groundcrew-read-only"
DEPLOY_ROLE = "arn:aws:iam::881050425883:role/groundcrew-deploy"


def _aws(tier: str | None, deploy_role: str | None = DEPLOY_ROLE) -> Connection:
    config: dict[str, Any] = {"role_arn": READ_ROLE, "external_id": "gc-1"}
    if tier:
        config["tier"] = tier
    if deploy_role:
        config["deploy_role_arn"] = deploy_role
    return _conn("aws", config)


def test_deploy_session_uses_the_deploy_role_and_allowlist() -> None:
    from app.connectors.aws import AWS_DEPLOY_OPERATIONS, DEPLOY_SESSION_NAME

    aws, fake = _aws("deploy"), FakeSessions()
    provider: CredentialsProvider = ConnectionCredentialsProvider([aws], sessions=fake)  # type: ignore[arg-type]
    provider.aws_deploy_session(aws.id)
    assert fake.assumed == [(DEPLOY_ROLE, "gc-1")]
    assert fake.kwargs == [
        {
            "operations": AWS_DEPLOY_OPERATIONS,
            "region": "us-east-2",
            "session_name": DEPLOY_SESSION_NAME,
        }
    ]


def test_aws_session_is_always_the_read_only_role() -> None:
    aws, fake = _aws("deploy"), FakeSessions()
    ConnectionCredentialsProvider([aws], sessions=fake).aws_session(aws.id)  # type: ignore[arg-type]
    assert fake.assumed == [(READ_ROLE, "gc-1")] and fake.kwargs == [{}]


@pytest.mark.parametrize("tier", [None, "observe"])
def test_deploy_session_refuses_an_observe_connection(tier: str | None) -> None:
    aws, fake = _aws(tier), FakeSessions()
    provider = ConnectionCredentialsProvider([aws], sessions=fake)  # type: ignore[arg-type]
    with pytest.raises(CredentialsError, match="Tier=observe, which cannot deploy"):
        provider.aws_deploy_session(aws.id)
    assert fake.assumed == []


def test_deploy_session_needs_a_verified_deploy_role() -> None:
    aws = _aws("deploy", deploy_role=None)
    provider = ConnectionCredentialsProvider([aws], sessions=FakeSessions())  # type: ignore[arg-type]
    with pytest.raises(CredentialsError, match="no verified deploy role"):
        provider.aws_deploy_session(aws.id)


def test_env_provider_deploy_session() -> None:
    fake = FakeSessions()
    env = {"GROUNDCREW_DEV_AWS_DEPLOY_ROLE_ARN": DEPLOY_ROLE, "GROUNDCREW_DEV_AWS_EXTERNAL_ID": "x"}
    EnvCredentialsProvider(env, sessions=fake).aws_deploy_session(uuid.uuid4())  # type: ignore[arg-type]
    assert fake.assumed == [(DEPLOY_ROLE, "x")]
    with pytest.raises(CredentialsError, match="GROUNDCREW_DEV_AWS_DEPLOY_ROLE_ARN"):
        EnvCredentialsProvider({}, sessions=fake).aws_deploy_session(uuid.uuid4())  # type: ignore[arg-type]


def test_deploy_and_read_sessions_are_guarded_by_their_own_allowlists() -> None:
    """Real AwsSessions with a stubbed STS: the deploy session may create an ECS service but
    never touch RDS or IAM or read a secret; the read session may not create anything."""
    from botocore.stub import Stubber

    from app.connectors.aws import AWS_DEPLOY_OPERATIONS, AwsSessions, ReadOnlyViolationError

    sessions = AwsSessions(Settings(_env_file=None), base=boto3.Session(region_name="us-east-2"))
    creds = {"AccessKeyId": "AKIAFAKE", "SecretAccessKey": "x", "SessionToken": "y"}
    sessions._assume = lambda *_a, **_k: creds  # type: ignore[method-assign]
    deploy = sessions.assume(DEPLOY_ROLE, "gc-1", operations=AWS_DEPLOY_OPERATIONS)
    read = sessions.assume(READ_ROLE, "gc-1")
    assert deploy is not read

    ecs = deploy.client("ecs")
    with Stubber(ecs) as stub:
        stub.add_response("create_service", {"service": {"serviceName": "shop"}})
        assert ecs.create_service(cluster="groundcrew-deploys", serviceName="shop")
    refused: list[tuple[Any, Callable[[Any], Any]]] = [
        (deploy.client("rds"), lambda c: c.delete_db_instance(DBInstanceIdentifier="x")),
        (
            deploy.client("iam"),
            lambda c: c.create_role(RoleName="x", AssumeRolePolicyDocument="{}"),
        ),
        (deploy.client("secretsmanager"), lambda c: c.get_secret_value(SecretId="x")),
        (read.client("ecs"), lambda c: c.create_service(serviceName="x")),
    ]
    for client, call in refused:
        with pytest.raises(ReadOnlyViolationError):
            call(client)
