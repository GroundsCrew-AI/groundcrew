"""AWS emergency actions (groundcrew-operate): plans read, execute writes once with its token and
only when the state still matches; real boto3 clients guarded by AWS_OPERATE_OPERATIONS."""

import json
import uuid
from typing import Any
from urllib.parse import quote

import pytest

from app.actions.aws import AwsEmergencyActions, pass_role_patterns, roles_outside
from app.actions.base import EmergencyActionError
from app.connectors.aws import AWS_OPERATE_OPERATIONS, AWS_READ_OPERATIONS
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
ROLE = f"arn:aws:iam::{ACCOUNT}:role"
TO_ECS = {"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}}


class World:
    """ECS and Secrets Manager state the fixture session answers from, and the writes."""

    def __init__(self) -> None:
        self.task_definition = f"{TD}:42"
        self.primary = "ecs-svc/2"
        self.stages = {BAD: ["AWSCURRENT"], GOOD: ["AWSPREVIOUS"]}
        self.calls: list[str] = []
        self.regions: list[str] = []
        self.writes: list[tuple[str, dict[str, Any]]] = []
        # The revision a rollback goes to, and the operate role's PassRole scope (the stack's
        # OperatePassRoleArns), read with the read-only role. None: GetRolePolicy fails.
        self.roles = {
            "executionRoleArn": f"{ROLE}/checkout-worker-task-execution",
            "taskRoleArn": f"{ROLE}/checkout-worker-task",
        }
        self.pass_roles: list[str] | None = [f"{ROLE}/checkout-*"]

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
            "ecs:DescribeTaskDefinition": lambda p: {
                "taskDefinition": {"taskDefinitionArn": p["taskDefinition"], **self.roles}
            },
            "iam:GetRolePolicy": (
                None
                if self.pass_roles is None
                else {
                    "RoleName": "groundcrew-operate",
                    "PolicyName": "groundcrew-operate",
                    # URL-encoded, as IAM sends it (botocore decodes it to a dict).
                    "PolicyDocument": quote(
                        json.dumps(
                            {
                                "Version": "2012-10-17",
                                "Statement": [
                                    {
                                        "Effect": "Allow",
                                        "Action": "ecs:UpdateService",
                                        "Resource": "*",
                                    },
                                    {
                                        "Effect": "Allow",
                                        "Action": "iam:PassRole",
                                        "Resource": self.pass_roles,
                                        "Condition": TO_ECS,
                                    },
                                ],
                            }
                        )
                    ),
                }
            ),
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
            config={
                "account_id": ACCOUNT,
                "external_id": "gc-1",
                "operate_role_arn": f"{ROLE}/groundcrew-operate",
            },
        )
        session.add(conn)
        await session.commit()
        cid = conn.id

    def client_for(conn: Connection, service: str, region: str) -> Any:
        world.regions.append(region)
        session = fixture_session(world.responses(), world.calls, allowed=AWS_OPERATE_OPERATIONS)
        return session.client(service, region_name=REGION)

    def read_client_for(conn: Connection, service: str, region: str) -> Any:
        session = fixture_session(world.responses(), world.calls, allowed=AWS_READ_OPERATIONS)
        return session.client(service, region_name=REGION)

    return AwsEmergencyActions(sf, client_for=client_for, read_client_for=read_client_for), cid


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
    assert plan.summary.endswith(f"back to version {GOOD}")  # the full id, never 8 characters
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
    real = AwsEmergencyActions(session_factory)  # the real session
    async with session_factory() as session:
        conn = await session.get_one(Connection, cid)
        conn.config = {"account_id": ACCOUNT, "external_id": "gc-1"}  # no operate role
        await session.commit()
    with pytest.raises(EmergencyActionError, match="EnableOperate=true"):
        await real.plan_force_redeploy(cid, CLUSTER, "checkout-worker")


async def test_rollback_plan_stops_when_the_revision_has_a_role_outside_pass_role(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    """The live bad-deploy-oom failure (AccessDenied on iam:PassRole at UpdateService) now
    stops at the plan, before anyone approves, and names the role."""
    world = World()
    actions, cid = await _setup(session_factory, workspace, world)
    world.pass_roles = ["arn:aws:iam::*:role/none"]  # the template's default
    with pytest.raises(
        EmergencyActionError,
        match=(
            r"rollback needs PassRole on .*role/checkout-worker-task-execution, "
            r".*role/checkout-worker-task; update the connection"
        ),
    ):
        await actions.plan_ecs_rollback_service(cid, CLUSTER, "checkout-worker")
    assert "iam:GetRolePolicy" in world.calls and world.writes == []
    world.pass_roles = [f"{ROLE}/checkout-worker-task-execution"]
    with pytest.raises(EmergencyActionError, match=r"PassRole on \S*role/checkout-worker-task;"):
        await actions.plan_ecs_rollback_service(cid, CLUSTER, "checkout-worker")


async def test_rollback_plan_goes_ahead_without_roles_or_without_a_readable_scope(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    world = World()
    actions, cid = await _setup(session_factory, workspace, world)
    world.roles, world.pass_roles = {}, ["arn:aws:iam::*:role/none"]
    plan = await actions.plan_ecs_rollback_service(cid, CLUSTER, "checkout-worker")
    assert plan.desired == f"{TD}:41" and "iam:GetRolePolicy" not in world.calls
    world.roles, world.pass_roles = World().roles, None  # GetRolePolicy fails: AWS decides
    plan = await actions.plan_ecs_rollback_service(cid, CLUSTER, "checkout-worker")
    assert plan.desired == f"{TD}:41"


def test_pass_role_patterns_only_counts_passing_to_ecs_tasks() -> None:
    document = {
        "Statement": [
            {"Effect": "Allow", "Action": "iam:PassRole", "Resource": "arn:a", "Condition": TO_ECS},
            {"Effect": "Allow", "Action": ["iam:PassRole"], "Resource": ["arn:b"]},
            {
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": "arn:c",
                "Condition": {"StringEquals": {"iam:PassedToService": "lambda.amazonaws.com"}},
            },
            {"Effect": "Deny", "Action": "iam:PassRole", "Resource": "arn:d"},
            {"Effect": "Allow", "Action": "ecs:UpdateService", "Resource": "arn:e"},
        ]
    }
    assert pass_role_patterns(document) == ["arn:a", "arn:b"]
    # IAM returns the document URL-encoded when it is not decoded for us.
    assert pass_role_patterns(quote(json.dumps(document))) == ["arn:a", "arn:b"]


def test_groundcrew_roles_are_never_passed_even_under_a_wide_pattern() -> None:
    roles = [f"{ROLE}/groundcrew-task-execution", f"{ROLE}/checkout-worker-task"]
    assert roles_outside(roles, ["*"]) == [f"{ROLE}/groundcrew-task-execution"]
    assert roles_outside(roles, [f"{ROLE}/checkout-*"]) == [f"{ROLE}/groundcrew-task-execution"]
    assert roles_outside([f"{ROLE}/checkout-api"], [f"{ROLE}/checkout-worker-*"]) == [
        f"{ROLE}/checkout-api"
    ]
