"""AWS emergency actions (groundcrew-operate): plans read, execute writes once with its token and
only when the state still matches; real boto3 clients guarded by AWS_OPERATE_OPERATIONS."""

import uuid
from typing import Any

import pytest

from app.actions.aws import AwsEmergencyActions
from app.actions.base import EmergencyActionError
from app.connectors.aws import AWS_OPERATE_OPERATIONS
from app.db.models import Connection, Workspace
from app.db.session import SessionFactory
from app.policy.engine import PolicyViolationError
from tests.aws_fixture import ACCOUNT, REGION, fixture_session
from tests.medic_fakes import approve_medic_action, medic_run

pytestmark = pytest.mark.anyio

CLUSTER = f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/checkout"
TD = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/checkout-worker"
SECRET = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:checkout/database-url-AbC123"
BAD = "bbbbbbbb-2222-4bad-8bad-bbbbbbbbbbbb"
GOOD = "aaaaaaaa-1111-4aaa-8aaa-aaaaaaaaaaaa"


class World:
    """ECS and Secrets Manager state the fixture session answers from, and the writes."""

    def __init__(self) -> None:
        self.task_definition = f"{TD}:42"
        self.primary = "ecs-svc/2"
        self.stages = {BAD: ["AWSCURRENT"], GOOD: ["AWSPREVIOUS"]}
        self.calls: list[str] = []
        self.writes: list[tuple[str, dict[str, Any]]] = []

    def responses(self) -> dict[str, Any]:
        def service(_: dict[str, Any]) -> dict[str, Any]:
            return {
                "services": [
                    {
                        "serviceName": "checkout-worker",
                        "taskDefinition": self.task_definition,
                        "deployments": [{"id": self.primary, "status": "PRIMARY"}],
                    }
                ]
            }

        def update(p: dict[str, Any]) -> dict[str, Any]:
            self.writes.append(("UpdateService", p))
            if p.get("taskDefinition"):
                self.task_definition = p["taskDefinition"]
            self.primary = "ecs-svc/3"
            return {"service": {"deployments": [{"id": self.primary, "status": "PRIMARY"}]}}

        def stage(p: dict[str, Any]) -> dict[str, Any]:
            self.writes.append(("UpdateSecretVersionStage", p))
            self.stages = {
                p["RemoveFromVersionId"]: ["AWSPREVIOUS"],
                p["MoveToVersionId"]: ["AWSCURRENT"],
            }
            return {"ARN": SECRET, "Name": "checkout/database-url"}

        return {
            "ecs:DescribeServices": service,
            "ecs:ListTaskDefinitions": {"taskDefinitionArns": [f"{TD}:42", f"{TD}:41", f"{TD}:40"]},
            "ecs:UpdateService": update,
            "secretsmanager:DescribeSecret": lambda _: {
                "ARN": SECRET,
                "Name": "checkout/database-url",
                "VersionIdsToStages": dict(self.stages),
            },
            "secretsmanager:UpdateSecretVersionStage": stage,
        }


async def _setup(
    sf: SessionFactory, ws: Workspace, world: World
) -> tuple[AwsEmergencyActions, uuid.UUID]:
    async with sf() as session:
        conn = Connection(
            workspace_id=ws.id,
            kind="aws",
            name="demo",
            status="connected",
            config={"account_id": ACCOUNT, "external_id": "gc-1"},
        )
        session.add(conn)
        await session.commit()
        cid = conn.id

    def client_for(conn: Connection, service: str) -> Any:
        session = fixture_session(world.responses(), world.calls, allowed=AWS_OPERATE_OPERATIONS)
        return session.client(service, region_name=REGION)

    return AwsEmergencyActions(sf, client_for=client_for), cid


async def test_ecs_rollback_goes_to_the_previous_revision_once(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    world = World()
    actions, cid = await _setup(session_factory, workspace, world)
    plan = await actions.plan_ecs_rollback_service(cid, CLUSTER, "checkout-worker")
    assert (plan.current, plan.desired) == (f"{TD}:42", f"{TD}:41")
    assert (
        plan.summary == "Point checkout-worker back at checkout-worker:41 (now checkout-worker:42)"
    )
    assert world.writes == []  # planning only reads
    run = await medic_run(session_factory, workspace)
    token = await approve_medic_action(
        session_factory, workspace, run, action=plan.action, plan_hash=plan.plan_hash()
    )
    result = await actions.execute(plan, token, run_id=run.id)
    assert result.ok and result.details == {
        "deployment_id": "ecs-svc/3",
        "task_definition": f"{TD}:41",
    }
    assert world.writes == [
        (
            "UpdateService",
            {"cluster": CLUSTER, "service": "checkout-worker", "taskDefinition": f"{TD}:41"},
        )
    ]
    with pytest.raises(PolicyViolationError, match="already used"):
        await actions.execute(plan, token, run_id=run.id)


async def test_nothing_is_written_when_the_service_moved_after_the_plan(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    world = World()
    actions, cid = await _setup(session_factory, workspace, world)
    plan = await actions.plan_ecs_rollback_service(cid, CLUSTER, "checkout-worker")
    world.task_definition = f"{TD}:43"  # someone deployed again
    run = await medic_run(session_factory, workspace)
    token = await approve_medic_action(
        session_factory, workspace, run, action=plan.action, plan_hash=plan.plan_hash()
    )
    result = await actions.execute(plan, token, run_id=run.id)
    assert not result.ok and "changed since the plan" in result.summary and world.writes == []


async def test_secret_rollback_moves_stages_and_never_reads_the_value(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    world = World()
    actions, cid = await _setup(session_factory, workspace, world)
    plan = await actions.plan_secret_rollback_version(cid, SECRET)
    assert (plan.current, plan.desired) == (BAD, GOOD)
    run = await medic_run(session_factory, workspace)
    token = await approve_medic_action(
        session_factory, workspace, run, action=plan.action, plan_hash=plan.plan_hash()
    )
    result = await actions.execute(plan, token, run_id=run.id)
    assert result.ok and world.stages == {BAD: ["AWSPREVIOUS"], GOOD: ["AWSCURRENT"]}
    assert world.writes[0][1] == {
        "SecretId": SECRET,
        "VersionStage": "AWSCURRENT",
        "MoveToVersionId": GOOD,
        "RemoveFromVersionId": BAD,
    }
    assert not any("GetSecretValue" in c for c in world.calls)


async def test_force_redeploy_refuses_when_a_deployment_already_started(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    world = World()
    actions, cid = await _setup(session_factory, workspace, world)
    plan = await actions.plan_force_redeploy(cid, CLUSTER, "checkout-worker")
    assert plan.current == "ecs-svc/2"
    world.primary = "ecs-svc/9"
    run = await medic_run(session_factory, workspace)
    token = await approve_medic_action(
        session_factory, workspace, run, action=plan.action, plan_hash=plan.plan_hash()
    )
    result = await actions.execute(plan, token, run_id=run.id)
    assert not result.ok and world.writes == []


async def test_plans_need_an_earlier_revision_and_an_operate_capable_connection(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    world = World()
    actions, cid = await _setup(session_factory, workspace, world)
    world.task_definition = f"{TD}:40"
    with pytest.raises(EmergencyActionError, match="no earlier task definition"):
        await actions.plan_ecs_rollback_service(cid, CLUSTER, "checkout-worker")
    real = AwsEmergencyActions(
        session_factory
    )  # the real session: the connection has no operate role
    with pytest.raises(EmergencyActionError, match="EnableOperate=true"):
        await real.plan_force_redeploy(cid, CLUSTER, "checkout-worker")
