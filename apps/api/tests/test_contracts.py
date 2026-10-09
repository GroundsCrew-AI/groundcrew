"""The Phase 3 contracts other phases call: CredentialsProvider and ResourceSink."""

import stat
import uuid
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from sqlalchemy import select

from app.config import get_settings
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

    def assume(self, role_arn: str, external_id: str) -> boto3.Session:
        self.assumed.append((role_arn, external_id))
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
