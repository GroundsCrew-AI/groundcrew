"""Per-connection AWS regions: an owner chooses where Groundcrew reads and acts in an account;
without a choice, DISCOVERY_REGIONS as before."""

import uuid

import httpx
import pytest
from sqlalchemy import update

from app.actions.base import EmergencyActionError
from app.config import get_settings
from app.connectors.aws import AwsReader, RegionNotAllowedError, arn_region, connection_regions
from app.db.models import Connection, Workspace
from app.db.session import SessionFactory
from tests.aws_fixture import ACCOUNT, REGION, fixture_session
from tests.test_actions_aws import World, _setup
from tests.test_connections_api import (  # noqa: F401 (fixtures)
    Member,
    alice,
    client,
    overrides,
    secrets_key,
)

pytestmark = pytest.mark.anyio


def test_a_connection_reads_its_own_regions_else_the_platform_default() -> None:
    settings = get_settings()
    assert connection_regions({}, settings) == list(settings.discovery_regions)
    assert connection_regions({"regions": ["eu-west-1", "ap-south-1"]}, settings) == [
        "eu-west-1",
        "ap-south-1",
    ]
    # Junk in the stored list is ignored, never used as a region.
    assert connection_regions({"regions": ["bogus", "eu-west-1"]}, settings) == ["eu-west-1"]
    assert arn_region("arn:aws:ecs:eu-west-1:1:cluster/x") == "eu-west-1"
    assert arn_region("arn:aws:iam::1:role/x") is None and arn_region("checkout") is None


def test_the_reader_refuses_regions_outside_the_connection() -> None:
    reader = AwsReader(fixture_session({}, []), get_settings(), ["eu-west-1"])
    assert reader.regions == ["eu-west-1"]
    reader.client("ecs", "eu-west-1")
    with pytest.raises(RegionNotAllowedError, match="not one of this connection's regions"):
        reader.client("ecs", "us-east-2")


async def test_actions_run_in_the_region_of_the_resource(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    world = World()
    actions, cid = await _setup(session_factory, workspace, world)
    async with session_factory() as session:
        conn = await session.get_one(Connection, cid)
        await session.execute(
            update(Connection)
            .where(Connection.id == cid)
            .values(config={**conn.config, "regions": [REGION, "eu-west-1"]})
        )
        await session.commit()
    cluster = f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/checkout"
    await actions.plan_force_redeploy(cid, cluster, "checkout-worker")
    assert world.regions == [REGION]
    other = f"arn:aws:ecs:ap-south-1:{ACCOUNT}:cluster/checkout"
    with pytest.raises(EmergencyActionError, match="ap-south-1 is not one of"):
        await actions.plan_force_redeploy(cid, other, "checkout-worker")


async def _aws(sf: SessionFactory, ws: uuid.UUID) -> uuid.UUID:
    async with sf() as session:
        conn = Connection(
            workspace_id=ws,
            kind="aws",
            name="AWS",
            status="connected",
            config={"account_id": ACCOUNT, "external_id": "gc-1", "region": "us-east-2"},
        )
        session.add(conn)
        await session.commit()
        return conn.id


async def test_the_owner_sets_and_clears_the_regions(
    client: httpx.AsyncClient,  # noqa: F811
    alice: Member,  # noqa: F811
    session_factory: SessionFactory,
) -> None:
    cid = await _aws(session_factory, alice.workspace_id)
    url = f"/connections/{cid}/aws/regions"
    bad = await client.put(url, json={"regions": ["mars-1"]}, headers=alice.headers)
    assert bad.status_code == 422
    saved = await client.put(
        url, json={"regions": ["EU-west-1", "eu-west-1", "us-east-2"]}, headers=alice.headers
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["regions"] == ["eu-west-1", "us-east-2"] and body["regions_chosen"] is True
    cleared = (await client.put(url, json={"regions": []}, headers=alice.headers)).json()
    assert cleared["regions_chosen"] is False
    assert cleared["regions"] == list(get_settings().discovery_regions)
    too_many = [f"eu-west-{i}" for i in range(1, 12)]
    assert (
        await client.put(url, json={"regions": too_many}, headers=alice.headers)
    ).status_code == 422
