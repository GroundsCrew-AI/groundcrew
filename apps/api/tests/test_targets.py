"""Deploy targets on fakes: plan, preflight, apply (push first, approved actions only), verify,
logs, and rollback for Kubernetes and AWS ECS. tests/test_targets_kind_live.py runs kind."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest

from app.config import Settings
from app.connectors.credentials import CredentialsProvider
from app.db.models import Workspace
from app.db.session import SessionFactory
from app.policy.actions import TARGET_APPLY, TARGET_ROLLBACK
from app.policy.engine import PolicyViolationError
from app.targets import EcsTarget, K8sTarget, PlanMismatchError
from app.targets.base import EnvValue

from .target_fakes import SECRET_VALUE, FakeAws, FakeKubeApi, FakeSandbox, approved_action, spec

pytestmark = pytest.mark.anyio

CONNECTION = uuid.UUID("00000000-0000-0000-0000-00000000c0de")


class NoCredentials:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError("the fakes replace credentials")


_db: dict[str, SessionFactory] = {}


@pytest.fixture(autouse=True)
def _test_db(session_factory: SessionFactory) -> None:
    """Targets spend approved actions in the test database."""
    _db["sf"] = session_factory


async def no_sleep(_seconds: float) -> None:
    return None


def http(status: int = 200, seen: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status if request.url.path != "/" else 404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def settings(**overrides: Any) -> Settings:
    return Settings(
        gc_kind_registry="kind-registry:5000", gc_verify_timeout_seconds=30, **overrides
    )


def k8s(
    events: list[str], api: FakeKubeApi | None = None, **kw: Any
) -> tuple[K8sTarget, FakeKubeApi]:
    api = api or FakeKubeApi(events)
    target = K8sTarget(
        settings(),
        cast(CredentialsProvider, NoCredentials()),
        CONNECTION,
        cluster="kind-groundcrew",
        api=api,
        sleep=no_sleep,
        session_factory=_db["sf"],
        **kw,
    )
    return target, api


def ecs(events: list[str], aws: FakeAws | None = None, **kw: Any) -> tuple[EcsTarget, FakeAws]:
    aws = aws or FakeAws(events)
    target = EcsTarget(
        settings(),
        cast(CredentialsProvider, NoCredentials()),
        CONNECTION,
        session=cast(Any, aws),
        sleep=no_sleep,
        session_factory=_db["sf"],
        **kw,
    )
    return target, aws


# --- Kubernetes ---------------------------------------------------------------------------


async def test_k8s_plan_lists_the_push_first_and_masks_secrets() -> None:
    target, _ = k8s([])
    plan = await target.plan(spec(uuid.uuid4(), CONNECTION))
    assert [(s.kind, s.action) for s in plan.steps] == [
        ("image", "push"),
        ("Namespace", "create"),
        ("RoleBinding", "create"),
        ("Secret", "create"),
        ("Deployment", "create"),
        ("Service", "create"),
        ("Ingress", "create"),
    ]
    assert plan.image == "kind-registry:5000/legacy-flask-app:3f2a9c1b7d4e"
    assert plan.expected_url == "http://legacy-flask-app.localhost"
    assert plan.previous_revision is None
    assert "hunter2" not in plan.model_dump_json()
    assert (
        'DATABASE_URL: "********"' in plan.rendered or "DATABASE_URL: '********'" in plan.rendered
    )
    assert "readinessProbe" in plan.rendered and "/health" in plan.rendered
    assert "gc-namespace-admin" in plan.rendered and "value: production" in plan.rendered


async def test_k8s_preflight_flags_missing_env_untested_images_and_foreign_names() -> None:
    target, api = k8s([])
    plan = await target.plan(spec(uuid.uuid4(), CONNECTION, env=[], smoke_passed=False))
    api.put("namespaces", None, {"metadata": {"name": "gc-legacy-flask-app", "labels": {}}})
    checks = {c.name: c for c in await target.preflight(plan)}
    assert checks["image"].status == "fail"
    assert (
        checks["env"].status == "fail" and checks["env"].detail == "Missing required: DATABASE_URL"
    )
    assert checks["name_collision"].status == "fail"


async def test_k8s_apply_needs_an_approved_action(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    events: list[str] = []
    target, _ = k8s(events)
    sandbox = FakeSandbox(events)
    run, _, token = await approved_action(
        session_factory, workspace, action=TARGET_APPLY, plan_hash="0" * 64
    )
    the_spec = spec(run.id, CONNECTION)
    plan = await target.plan(the_spec)

    with pytest.raises(PolicyViolationError, match="needs an approved action"):
        await target.apply(plan, the_spec, cast(Any, None), sandbox)
    with pytest.raises(PolicyViolationError, match="different plan"):
        await target.apply(plan, the_spec, token, sandbox)  # type: ignore[arg-type]
    assert events == []

    _, _, token = await approved_action(
        session_factory, workspace, action=TARGET_APPLY, plan_hash=plan.plan_hash(), run=run
    )
    assert token is not None
    await target.apply(plan, the_spec, token, sandbox)
    with pytest.raises(PolicyViolationError, match="already used"):
        await target.apply(plan, the_spec, token, sandbox)


async def test_k8s_apply_pushes_first_then_writes_in_order(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    events: list[str] = []
    target, api = k8s(events)
    sandbox = FakeSandbox(events)
    run, _, _ = await approved_action(
        session_factory, workspace, action=TARGET_APPLY, plan_hash="0" * 64
    )
    the_spec = spec(run.id, CONNECTION)
    plan = await target.plan(the_spec)
    _, _, token = await approved_action(
        session_factory, workspace, action=TARGET_APPLY, plan_hash=plan.plan_hash(), run=run
    )
    assert token is not None
    deployment = await target.apply(plan, the_spec, token, sandbox)

    assert events == [
        "push gc-build/legacy-flask-app:3f2a9c1b7d4e"
        " -> kind-registry:5000/legacy-flask-app:3f2a9c1b7d4e",
        "create namespaces/gc-legacy-flask-app",
        "create rolebindings/groundcrew-deployer",
        "apply secrets/legacy-flask-app-env",
        "apply deployments/legacy-flask-app",
        "apply services/legacy-flask-app",
        "apply ingresses/legacy-flask-app",
    ]
    namespace = api.objects[("namespaces", None, "gc-legacy-flask-app")]
    assert namespace["metadata"]["labels"]["project"] == "groundcrew"
    assert namespace["metadata"]["labels"]["groundcrew/deploy"] == str(run.id)
    secret = api.objects[("secrets", "gc-legacy-flask-app", "legacy-flask-app-env")]
    assert secret["stringData"] == {"DATABASE_URL": SECRET_VALUE}
    assert deployment.revision == "1" and deployment.previous_revision is None
    assert deployment.live_url == "http://legacy-flask-app.localhost"


async def test_k8s_apply_refuses_a_spec_that_is_not_the_plan(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    events: list[str] = []
    target, _ = k8s(events)
    run, _, _ = await approved_action(
        session_factory, workspace, action=TARGET_APPLY, plan_hash="0" * 64
    )
    plan = await target.plan(spec(run.id, CONNECTION))
    _, _, token = await approved_action(
        session_factory, workspace, action=TARGET_APPLY, plan_hash=plan.plan_hash(), run=run
    )
    other = spec(run.id, CONNECTION, env=[EnvValue(name="MODE", value="debug")])
    with pytest.raises(PlanMismatchError, match="env vars"):
        await target.apply(plan, other, token, FakeSandbox(events))  # type: ignore[arg-type]
    assert events == []


async def _deployed_k8s(
    sf: SessionFactory, ws: Workspace, events: list[str], **kw: Any
) -> tuple[K8sTarget, FakeKubeApi, Any]:
    target, api = k8s(events, **kw)
    run, _, _ = await approved_action(sf, ws, action=TARGET_APPLY, plan_hash="0" * 64)
    the_spec = spec(run.id, CONNECTION)
    plan = await target.plan(the_spec)
    _, _, token = await approved_action(
        sf, ws, action=TARGET_APPLY, plan_hash=plan.plan_hash(), run=run
    )
    deployment = await target.apply(plan, the_spec, token, FakeSandbox(events))  # type: ignore[arg-type]
    return target, api, deployment


async def test_k8s_verify_checks_rollout_and_health_through_the_ingress(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    seen: list[httpx.Request] = []
    target, _, deployment = await _deployed_k8s(
        session_factory, workspace, [], http=http(200, seen)
    )
    health = await target.verify(deployment)
    assert health.ok, health
    assert [(c.name, c.ok) for c in health.checks] == [
        ("rollout", True),
        ("health", True),
        ("root", False),
    ]
    assert health.live_url == "http://legacy-flask-app.localhost"
    assert (
        seen[0].url == "http://127.0.0.1/health"
        and seen[0].headers["host"] == "legacy-flask-app.localhost"
    )


async def test_k8s_verify_stops_early_on_an_image_pull_error(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    api = FakeKubeApi([])
    api.ready = False
    api.pods = [
        {
            "metadata": {"name": "legacy-flask-app-abc"},
            "status": {
                "containerStatuses": [
                    {
                        "state": {
                            "waiting": {
                                "reason": "ImagePullBackOff",
                                "message": "pull access denied",
                            }
                        },
                        "restartCount": 0,
                    }
                ]
            },
        }
    ]
    target, _, deployment = await _deployed_k8s(
        session_factory, workspace, [], api=api, http=http()
    )
    health = await target.verify(deployment)
    assert not health.ok
    assert health.checks[0].detail.startswith("legacy-flask-app-abc: ImagePullBackOff")
    assert health.logs_tail and "listening on 8000" in health.logs_tail


async def test_k8s_logs_come_from_the_pods() -> None:
    target, _ = k8s([])
    from app.targets.base import Deployment

    deployment = Deployment(
        target="k8s",
        connection_id=CONNECTION,
        run_id=uuid.uuid4(),
        app="legacy-flask-app",
        image="i",
        revision="1",
        previous_revision=None,
        location="gc-legacy-flask-app",
        port=8000,
        health_path="/health",
    )
    lines = await target.logs(deployment, datetime.now(UTC) - timedelta(minutes=5))
    assert [(line.source, line.message) for line in lines] == [
        ("legacy-flask-app-abc", "booting"),
        ("legacy-flask-app-abc", "listening on 8000"),
    ]


async def test_k8s_rollback_of_a_first_deploy_deletes_the_namespace_only_with_an_emergency_approval(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    events: list[str] = []
    target, api, deployment = await _deployed_k8s(session_factory, workspace, events, http=http())
    plan = await target.rollback_plan(deployment)
    assert [(s.kind, s.action) for s in plan.steps] == [("Namespace", "delete")]
    run_id = deployment.run_id
    from app.db.models import Run

    async with session_factory() as session:
        run = await session.get(Run, run_id)
    # An apply approval for the same plan hash is not a rollback approval.
    with pytest.raises(PolicyViolationError):
        _, _, wrong = await approved_action(
            session_factory, workspace, action=TARGET_APPLY, plan_hash=plan.plan_hash(), run=run
        )
        await target.rollback(plan, wrong)  # type: ignore[arg-type]
    assert ("namespaces", None, "gc-legacy-flask-app") in api.objects

    _, _, token = await approved_action(
        session_factory, workspace, action=TARGET_ROLLBACK, plan_hash=plan.plan_hash(), run=run
    )
    rolled = await target.rollback(plan, token)  # type: ignore[arg-type]
    assert rolled.status == "deleted"
    assert events[-1] == "delete namespaces/gc-legacy-flask-app"


async def test_k8s_rollback_returns_to_the_previous_template(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    events: list[str] = []
    target, api, _ = await _deployed_k8s(session_factory, workspace, events, http=http())
    # A second deploy with a new tag.
    run, _, _ = await approved_action(
        session_factory, workspace, action=TARGET_APPLY, plan_hash="0" * 64
    )
    second_spec = spec(run.id, CONNECTION, tag="bbbbbbbbbbbb")
    plan = await target.plan(second_spec)
    assert plan.previous_revision == "1"
    assert [s.action for s in plan.steps][1:3] == ["unchanged", "unchanged"]
    _, _, token = await approved_action(
        session_factory, workspace, action=TARGET_APPLY, plan_hash=plan.plan_hash(), run=run
    )
    second = await target.apply(plan, second_spec, token, FakeSandbox(events))  # type: ignore[arg-type]
    assert second.revision == "2" and second.previous_revision == "1"

    back = await target.rollback_plan(second)
    assert back.rollback_to_revision == "1"
    _, _, token = await approved_action(
        session_factory, workspace, action=TARGET_ROLLBACK, plan_hash=back.plan_hash(), run=run
    )
    rolled = await target.rollback(back, token)  # type: ignore[arg-type]
    deployment = api.objects[("deployments", "gc-legacy-flask-app", "legacy-flask-app")]
    image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image.endswith(":3f2a9c1b7d4e") and rolled.image == image
    assert rolled.status == "rolled_back" and rolled.revision == "3"


# --- AWS ECS ------------------------------------------------------------------------------


async def test_ecs_plan_creates_the_repository_then_pushes_and_masks_secrets() -> None:
    target, aws = ecs([])
    plan = await target.plan(spec(uuid.uuid4(), CONNECTION))
    assert [(s.kind, s.action) for s in plan.steps] == [
        ("ecr:Repository", "create"),
        ("image", "push"),
        ("logs:LogGroup", "create"),
        ("ec2:SecurityGroup", "create"),
        ("secretsmanager:Secret", "create"),
        ("ecs:TaskDefinition", "create"),
        ("ecs:Service", "create"),
    ]
    assert (
        plan.image
        == "881050425883.dkr.ecr.us-east-2.amazonaws.com/groundcrew-legacy-flask-app:3f2a9c1b7d4e"
    )
    assert plan.steps[3].detail == "tcp/8000 from 0.0.0.0/0"
    assert "hunter2" not in plan.model_dump_json()
    rendered = json.loads(plan.rendered)
    container = rendered["taskDefinition"]["containerDefinitions"][0]
    assert container["secrets"] == [
        {"name": "DATABASE_URL", "valueFrom": "groundcrew/legacy-flask-app/DATABASE_URL"}
    ]
    assert container["environment"] == [{"name": "MODE", "value": "production"}]
    assert rendered["taskDefinition"]["family"] == "groundcrew-legacy-flask-app"
    # Planning only reads.
    assert all(op.split(":")[1].startswith(("describe", "get", "list")) for op in aws.ops())


async def test_ecs_apply_order_tags_and_no_deregistering(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    events: list[str] = []
    target, aws = ecs(events)
    run, _, _ = await approved_action(
        session_factory, workspace, action=TARGET_APPLY, plan_hash="0" * 64
    )
    the_spec = spec(run.id, CONNECTION)
    plan = await target.plan(the_spec)
    with pytest.raises(PolicyViolationError):
        await target.apply(plan, the_spec, cast(Any, None), FakeSandbox(events))
    assert events == []
    _, _, token = await approved_action(
        session_factory, workspace, action=TARGET_APPLY, plan_hash=plan.plan_hash(), run=run
    )
    sandbox = FakeSandbox(events)
    deployment = await target.apply(plan, the_spec, token, sandbox)  # type: ignore[arg-type]

    assert events == [
        "create ecr groundcrew-legacy-flask-app",
        "push gc-build/legacy-flask-app:3f2a9c1b7d4e -> "
        "881050425883.dkr.ecr.us-east-2.amazonaws.com/groundcrew-legacy-flask-app:3f2a9c1b7d4e",
        "create log group /groundcrew/legacy-flask-app",
        "create sg groundcrew-legacy-flask-app",
        "create secret groundcrew/legacy-flask-app/DATABASE_URL",
        "register groundcrew-legacy-flask-app:1",
        "create service legacy-flask-app",
    ]
    assert sandbox.pushed[0][1].login is not None
    assert aws.secrets["groundcrew/legacy-flask-app/DATABASE_URL"] == SECRET_VALUE
    calls = {(s, o): kw for s, o, kw in aws.calls}
    tag = {"Key": "groundcrew:deploy", "Value": str(run.id)}
    assert tag in calls[("ecr", "create_repository")]["tags"]
    assert calls[("logs", "create_log_group")]["tags"]["project"] == "groundcrew"
    assert tag in calls[("ec2", "create_security_group")]["TagSpecifications"][0]["Tags"]
    assert tag in calls[("secretsmanager", "create_secret")]["Tags"]
    ingress = calls[("ec2", "authorize_security_group_ingress")]["IpPermissions"][0]
    assert (ingress["FromPort"], ingress["IpRanges"][0]["CidrIp"]) == (8000, "0.0.0.0/0")
    registered = calls[("ecs", "register_task_definition")]
    assert registered["containerDefinitions"][0]["secrets"][0]["valueFrom"].startswith(
        "arn:aws:secretsmanager:"
    )
    assert {"key": "project", "value": "groundcrew"} in registered["tags"]
    assert not any("deregister" in op for op in aws.ops())
    assert not any(op in ("secretsmanager:get_secret_value",) for op in aws.ops())
    assert deployment.revision and deployment.revision.endswith("groundcrew-legacy-flask-app:1")
    assert deployment.external_id and deployment.external_id.endswith(
        "service/groundcrew-deploys/legacy-flask-app"
    )


async def test_ecs_security_group_source_is_configurable() -> None:
    aws = FakeAws([])
    target = EcsTarget(
        settings(gc_ingress_cidr="198.51.100.7/32"),
        cast(CredentialsProvider, NoCredentials()),
        CONNECTION,
        session=cast(Any, aws),
        sleep=no_sleep,
        session_factory=_db["sf"],
    )
    plan = await target.plan(spec(uuid.uuid4(), CONNECTION))
    assert plan.steps[3].detail == "tcp/8000 from 198.51.100.7/32"
    with pytest.raises(ValueError, match="does not appear to be"):
        settings(gc_ingress_cidr="everyone")


async def _deployed_ecs(
    sf: SessionFactory, ws: Workspace, events: list[str], **kw: Any
) -> tuple[EcsTarget, FakeAws, Any]:
    target, aws = ecs(events, **kw)
    run, _, _ = await approved_action(sf, ws, action=TARGET_APPLY, plan_hash="0" * 64)
    the_spec = spec(run.id, CONNECTION)
    plan = await target.plan(the_spec)
    _, _, token = await approved_action(
        sf, ws, action=TARGET_APPLY, plan_hash=plan.plan_hash(), run=run
    )
    deployment = await target.apply(plan, the_spec, token, FakeSandbox(events))  # type: ignore[arg-type]
    return target, aws, deployment


async def test_ecs_verify_finds_the_task_ip_and_checks_health(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    seen: list[httpx.Request] = []
    target, aws, deployment = await _deployed_ecs(
        session_factory, workspace, [], http=http(200, seen)
    )
    before = len(aws.calls)
    health = await target.verify(deployment)
    describes = [c for c in aws.calls[before:] if c[1] == "describe_services"]
    # ECS first reports the new deployment as 0 of 0 tasks; verify waits past that.
    assert len(describes) == 3
    assert health.ok, health
    assert health.live_url == "http://203.0.113.9:8000"
    assert str(seen[0].url) == "http://203.0.113.9:8000/health"


async def test_ecs_verify_reports_a_failed_rollout_with_logs(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    aws = FakeAws([])
    aws.rollout = "FAILED"
    target, _, deployment = await _deployed_ecs(
        session_factory, workspace, [], aws=aws, http=http()
    )
    health = await target.verify(deployment)
    assert not health.ok and health.checks[0].name == "rollout"
    assert "tasks failed to start" in health.checks[0].detail
    assert health.logs_tail and "listening on 8000" in health.logs_tail


async def test_ecs_rollback_to_the_previous_task_definition(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    events: list[str] = []
    target, aws, _ = await _deployed_ecs(session_factory, workspace, events, http=http())
    run, _, _ = await approved_action(
        session_factory, workspace, action=TARGET_APPLY, plan_hash="0" * 64
    )
    second_spec = spec(run.id, CONNECTION, tag="bbbbbbbbbbbb")
    plan = await target.plan(second_spec)
    assert plan.previous_revision and plan.previous_revision.endswith(":1")
    assert [s.action for s in plan.steps] == [
        "unchanged",
        "push",
        "unchanged",
        "unchanged",
        "update",
        "create",
        "update",
    ]
    _, _, token = await approved_action(
        session_factory, workspace, action=TARGET_APPLY, plan_hash=plan.plan_hash(), run=run
    )
    second = await target.apply(plan, second_spec, token, FakeSandbox(events))  # type: ignore[arg-type]
    assert second.revision and second.revision.endswith(":2")

    back = await target.rollback_plan(second)
    assert back.steps[0].action == "update" and back.rollback_to_revision == plan.previous_revision
    _, _, token = await approved_action(
        session_factory, workspace, action=TARGET_ROLLBACK, plan_hash=back.plan_hash(), run=run
    )
    rolled = await target.rollback(back, token)  # type: ignore[arg-type]
    assert aws.services["legacy-flask-app"]["taskDefinition"] == plan.previous_revision
    assert rolled.image.endswith(":3f2a9c1b7d4e") and rolled.status == "rolled_back"
    assert not any("deregister" in op for op in aws.ops())


async def test_ecs_rollback_of_a_first_deploy_deletes_the_service(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    events: list[str] = []
    target, aws, deployment = await _deployed_ecs(session_factory, workspace, events, http=http())
    plan = await target.rollback_plan(deployment)
    assert [(s.kind, s.action) for s in plan.steps] == [("ecs:Service", "delete")]
    from app.db.models import Run

    async with session_factory() as session:
        run = await session.get(Run, deployment.run_id)
    _, _, token = await approved_action(
        session_factory, workspace, action=TARGET_ROLLBACK, plan_hash=plan.plan_hash(), run=run
    )
    rolled = await target.rollback(plan, token)  # type: ignore[arg-type]
    assert rolled.status == "deleted" and "legacy-flask-app" not in aws.services
    assert events[-2:] == ["update service legacy-flask-app", "delete service legacy-flask-app"]


async def test_ecs_discover_and_logs(session_factory: SessionFactory, workspace: Workspace) -> None:
    target, _, deployment = await _deployed_ecs(session_factory, workspace, [], http=http())
    found = await target.discover()
    assert [(r.type, r.name) for r in found.resources] == [("ecs:service", "legacy-flask-app")]
    assert found.resources[0].external_id == deployment.external_id
    lines = await target.logs(deployment, datetime.now(UTC) - timedelta(minutes=5))
    assert lines[0].message == "listening on 8000"


async def test_k8s_preflight_waits_for_a_terminating_namespace() -> None:
    target, api = k8s([])
    plan = await target.plan(spec(uuid.uuid4(), CONNECTION))
    api.put(
        "namespaces",
        None,
        {
            "metadata": {"name": "gc-legacy-flask-app", "labels": {"project": "groundcrew"}},
            "status": {"phase": "Terminating"},
        },
    )
    checks = {c.name: c for c in await target.preflight(plan)}
    assert checks["name_collision"].status == "fail"
    assert "still being deleted" in checks["name_collision"].detail


async def test_k8s_verify_stops_early_when_pods_cannot_be_created(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    api = FakeKubeApi([])
    api.ready = False
    target, _, deployment = await _deployed_k8s(
        session_factory, workspace, [], api=api, http=http()
    )
    stored = api.objects[("deployments", "gc-legacy-flask-app", "legacy-flask-app")]
    stored["status"]["conditions"] = [
        {"type": "ReplicaFailure", "status": "True", "reason": "FailedCreate",
         "message": 'pods "x" is forbidden: exceeded quota'}
    ]  # fmt: skip
    health = await target.verify(deployment)
    assert not health.ok
    assert health.checks[0].detail.startswith("FailedCreate: pods")
