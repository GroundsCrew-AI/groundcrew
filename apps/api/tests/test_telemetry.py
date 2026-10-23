"""Telemetry readers and collect_evidence (Phase 5 M1) on fixtures shaped like the three
checkout-demo scenarios. Real boto3 clients with the read-only guard; no network."""

import re
import uuid
from datetime import datetime
from typing import Any

import httpx
import pytest

from app.config import get_settings
from app.connectors.aws import AwsReader
from app.connectors.github import GitHubApp
from app.connectors.kubernetes import KubeReader
from app.connectors.vercel import VercelClient
from app.db.models import Connection, Edge, Resource, Workspace
from app.db.session import SessionFactory, get_session_factory
from app.main import app
from app.telemetry import cloudwatch_logs, ecs, github, rds, secrets, vercel
from app.telemetry.collect import GraphEvidenceCollector, collect
from app.telemetry.evidence import MAX_LINES_PER_SOURCE, Line, line, render
from app.telemetry.timeline import timeline
from tests.aws_fixture import ACCOUNT, REGION, fixture_session
from tests.discovery_helpers import INSTALLATION, github_app, vercel_client
from tests.telemetry_fixtures import (
    BAD_SHA,
    CLUSTER,
    REPO,
    SECRET_ARN,
    SINCE,
    T0,
    TD,
    UNTIL,
    at,
    env_aws,
    index_github,
    index_logs,
    index_rds,
    oom_aws,
    oom_github,
    quiet_github,
)
from tests.test_api import Member, _member, _settings

pytestmark = pytest.mark.anyio

LINE = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+\+00:00 \| \w+ \| \S.* \| .+$")


def _reader(responses: dict[str, Any], calls: list[str] | None = None) -> AwsReader:
    return AwsReader(fixture_session(responses, calls), get_settings())


def _messages(lines: list[Line]) -> str:
    return "\n".join(item.message for item in lines)


# --- readers ------------------------------------------------------------------------------


def test_ecs_reader_finds_oom_kills_and_the_failed_rollout() -> None:
    out = ecs.read_service(_reader(oom_aws()), REGION, CLUSTER, "checkout-worker", SINCE, UNTIL)
    text = _messages(out.lines)
    assert text.count("exit code 137") == 3 and "[OOM]" in text
    assert "OutOfMemoryError: Container killed due to memory usage" in text
    assert "rollout FAILED task definition checkout-worker:42" in text
    assert "failed tasks 3" in text and "circuit breaker" in text
    assert "now: 0/1 tasks running" in text
    # The new deployment is a change marker; the old one (created long before) is not.
    assert [c.ref.rsplit("/", 1)[-1] for c in out.changes] == ["checkout-worker:42"]


def test_ecs_reader_shows_retirement_then_failed_health_checks() -> None:
    out = ecs.read_service(_reader(env_aws()), REGION, CLUSTER, "checkout-api", SINCE, UNTIL)
    stopped = [x.message for x in out.lines if " stopped: " in x.message]
    assert "Simulated Fargate task retirement" in stopped[0]
    assert all("failed container health checks" in s for s in stopped[1:])


def _flask_definition(rev: int, secret_suffix: str, image: str = "flask:af37") -> dict[str, Any]:
    return {
        "taskDefinitionArn": f"{TD}/legacy-flask-app:{rev}",
        "cpu": "256",
        "memory": "512",
        "containerDefinitions": [
            {
                "name": "app",
                "image": image,
                "command": ["gunicorn", "-b", "0.0.0.0:8000", "app:app"],
                "environment": [{"name": "FLASK_ENV", "value": "production"}],
                "secrets": [
                    {
                        "name": "DATABASE_URL",
                        "valueFrom": f"{SECRET_ARN.rsplit('-', 1)[0]}-{secret_suffix}",
                    }
                ],
            }
        ],
    }


def _flask_aws(denied: bool = False) -> dict[str, Any]:
    """An environment rebuilt by Terraform: revision 3 moves DATABASE_URL to a new secret, keeps
    the image and command; one task runs while its load balancer target times out."""
    group = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/flask/abc"
    service = {
        "serviceArn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/checkout/legacy-flask-app",
        "serviceName": "legacy-flask-app",
        "status": "ACTIVE",
        "runningCount": 1,
        "desiredCount": 1,
        "events": [],
        "loadBalancers": [{"targetGroupArn": group, "containerName": "app", "containerPort": 8000}],
        "deployments": [
            {
                "id": "ecs-svc/3",
                "status": "PRIMARY",
                "taskDefinition": f"{TD}/legacy-flask-app:3",
                "desiredCount": 1,
                "runningCount": 1,
                "rolloutState": "COMPLETED",
                "createdAt": at(-20),
                "updatedAt": at(-19),
            }
        ],
    }
    running = {
        "taskArn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/checkout/run111",
        "taskDefinitionArn": f"{TD}/legacy-flask-app:3",
        "lastStatus": "RUNNING",
        "startedAt": at(-19),
        "healthStatus": "UNKNOWN",
        "containers": [{"name": "app"}],
    }
    definitions = {
        f"{TD}/legacy-flask-app:3": _flask_definition(3, "eJVGhT"),
        f"{TD}/legacy-flask-app:2": _flask_definition(2, "oJsbmj"),
    }
    elb: dict[str, Any] = {
        "elbv2:DescribeTargetGroups": {
            "TargetGroups": [
                {
                    "TargetGroupArn": group,
                    "TargetGroupName": "flask",
                    "HealthCheckProtocol": "HTTP",
                    "HealthCheckPort": "traffic-port",
                    "HealthCheckPath": "/health",
                    "HealthCheckTimeoutSeconds": 5,
                    "HealthCheckIntervalSeconds": 30,
                    "Matcher": {"HttpCode": "200"},
                }
            ]
        },
        "elbv2:DescribeTargetHealth": {
            "TargetHealthDescriptions": [
                {
                    "Target": {"Id": "10.0.1.23", "Port": 8000},
                    "TargetHealth": {
                        "State": "unhealthy",
                        "Reason": "Target.Timeout",
                        "Description": "Request timed out",
                    },
                }
            ]
        },
    }
    if denied:
        elb = {
            "elbv2:DescribeTargetGroups": {
                "Error": {"Code": "AccessDenied", "Message": "not allowed"}
            }
        }
    return {
        "ecs:DescribeServices": {"services": [service], "failures": []},
        "ecs:ListTasks": lambda p: {
            "taskArns": [running["taskArn"]] if p.get("desiredStatus") == "RUNNING" else []
        },
        "ecs:DescribeTasks": {"tasks": [running], "failures": []},
        "ecs:DescribeTaskDefinition": lambda p: {
            "taskDefinition": definitions[p["taskDefinition"]]
        },
        **elb,
    }


def test_ecs_reader_reads_target_and_task_health_and_what_a_deploy_changed() -> None:
    out = ecs.read_service(_reader(_flask_aws()), REGION, CLUSTER, "legacy-flask-app", SINCE, UNTIL)
    text = _messages(out.lines)
    assert (
        "load balancer target group flask: 0/1 targets healthy (health check HTTP /health on "
        "port traffic-port, expects 200, timeout 5 s every 30 s); 10.0.1.23:8000 unhealthy "
        "Target.Timeout Request timed out" in text
    )
    assert "task run111 running for 34 min, health UNKNOWN" in text
    assert "container app health UNKNOWN (no container health check)" in text
    (diff,) = [x for x in out.lines if x.message.startswith("task definition legacy-flask-app:3")]
    assert diff.at == at(-20)
    assert diff.message.startswith(
        "task definition legacy-flask-app:3 vs legacy-flask-app:2 (same images and commands: "
        "this deploy shipped no new application code): container app: secrets DATABASE_URL now "
        "from another secret; runs 'gunicorn -b 0.0.0.0:8000 app:app'"
    )
    # Names only: no environment value or secret source in the comparison.
    assert "production" not in diff.message and "eJVGhT" not in diff.message


def test_ecs_reader_says_when_a_deploy_reused_an_existing_revision() -> None:
    """A forced redeploy after a secret change: ECS no longer lists the old deployment, and
    revision 3 was registered long before. Comparing with revision 2 would invent a change."""
    responses = _flask_aws()
    old = responses["ecs:DescribeTaskDefinition"]

    def describe(p: dict[str, Any]) -> dict[str, Any]:
        found: dict[str, Any] = old(p)
        found["taskDefinition"]["registeredAt"] = at(-600)
        return found

    responses["ecs:DescribeTaskDefinition"] = describe
    out = ecs.read_service(_reader(responses), REGION, CLUSTER, "legacy-flask-app", SINCE, UNTIL)
    (diff,) = [x for x in out.lines if x.message.startswith("task definition legacy-flask-app:3")]
    assert diff.message == (
        f"task definition legacy-flask-app:3 was registered at {at(-600).isoformat()}, before "
        "this deploy: it reused an existing revision (no task definition change); its new tasks "
        "read secrets and environment again when they start"
    )


def test_ecs_compare_names_changed_code_and_settings() -> None:
    old = _flask_definition(2, "oJsbmj")
    new = _flask_definition(3, "oJsbmj", image="flask:b001")
    new["containerDefinitions"][0]["environment"] = [{"name": "WORKERS", "value": "4"}]
    new["memory"] = "1024"
    text = ecs.compare(new, old)
    assert "no new application code" not in text
    assert "image flask:af37 -> flask:b001" in text
    assert "environment +WORKERS, -FLASK_ENV" in text and "task memory 512 -> 1024" in text


def test_ecs_reader_keeps_going_when_a_health_read_is_refused() -> None:
    out = ecs.read_service(
        _reader(_flask_aws(denied=True)), REGION, CLUSTER, "legacy-flask-app", SINCE, UNTIL
    )
    text = _messages(out.lines)
    assert "load balancer target health not read: AccessDenied: not allowed" in text
    assert "now: 1/1 tasks running" in text and "task run111 running" in text


def test_log_reader_keeps_errors_with_context_and_redacts() -> None:
    out = cloudwatch_logs.read_log_group(
        _reader(env_aws()), REGION, "/ecs/checkout-api", SINCE, UNTIL
    )
    text = _messages(out.lines)
    assert text.count("health check failed: database unreachable") == 2
    assert "hunter2" not in text and "checkout-orders-db.invalid" in text


def test_log_reader_summarizes_latency_per_minute_and_path() -> None:
    out = cloudwatch_logs.read_log_group(
        _reader(index_logs()), REGION, "/ecs/checkout-api", SINCE, UNTIL
    )
    summaries = [x.message for x in out.lines if x.message.startswith("latency summary")]
    search = [s for s in summaries if "path=/orders/search" in s]
    assert search and "p50=3ms" in search[0] and "p50=9" in search[-1]
    # Individual request lines are folded into the summaries, not repeated.
    assert not any('"latency_ms"' in x.message for x in out.lines)


def test_rds_reader_marks_the_cpu_and_latency_spike() -> None:
    out = rds.read_db(_reader(index_rds()), REGION, "checkout-orders-db", SINCE, UNTIL)
    spikes = [x for x in out.lines if "[SPIKE]" in x.message]
    assert any(x.message.startswith("CPUUtilization 88") for x in spikes)
    assert any(x.message.startswith("ReadLatency 25") for x in spikes)
    assert min(x.at for x in spikes) == at(-5)
    assert not any("DatabaseConnections" in x.message and "[SPIKE]" in x.message for x in out.lines)


def test_secrets_reader_reads_metadata_never_the_value() -> None:
    calls: list[str] = []
    out = secrets.read_secret(_reader(env_aws(), calls), REGION, SECRET_ARN, SINCE, UNTIL)
    text = _messages(out.lines)
    assert "value last changed (inside the incident window)" in text
    assert "AWSCURRENT=bbbbbbbb" in text and "AWSPREVIOUS=aaaaaaaa" in text
    assert calls == ["secretsmanager:DescribeSecret"]
    assert [c.kind for c in out.changes] == ["secret_change"]
    assert out.changes[0].ref.endswith(":bbbbbbbb-2222-bad")


async def test_github_reader_ties_the_commit_to_its_files_and_the_failed_deploy() -> None:
    out = await github.read_repo(
        github_app(extra_routes=oom_github()), INSTALLATION, REPO, SINCE, UNTIL
    )
    text = _messages(out.lines)
    assert f"commit {BAD_SHA[:7]} by dev: Tune worker memory | files: worker/ecs-task.json" in text
    assert "workflow run deploy.yml #41 (push) completed failure" in text
    assert {c.kind for c in out.changes} == {"commit", "deploy"}


async def test_vercel_reader_lists_production_deployments_in_the_window() -> None:
    routes = {
        "GET /v6/deployments": {
            "deployments": [
                {
                    "uid": "dpl_new",
                    "url": "web-x.vercel.app",
                    "state": "READY",
                    "created": int(at(-3).timestamp() * 1000),
                    "meta": {"githubCommitSha": BAD_SHA},
                },
                {
                    "uid": "dpl_old",
                    "url": "web-y.vercel.app",
                    "state": "READY",
                    "created": int(at(-300).timestamp() * 1000),
                },
            ]
        }
    }
    out = await vercel.read_project(vercel_client(routes), "prj_1", "checkout-web", SINCE, UNTIL)
    assert [x.message.split()[2] for x in out.lines] == ["web-x.vercel.app"]
    assert out.changes[0].url == "https://web-x.vercel.app"


# --- lines, caps, timeline ------------------------------------------------------------------


def test_render_keeps_the_newest_lines_and_says_what_was_cut() -> None:
    lines = [line(at(i / 10), "ecs", "svc", f"event {i}") for i in range(MAX_LINES_PER_SOURCE + 7)]
    text = render(lines)
    rows = text.splitlines()
    assert rows[0] == "... 7 more lines before these"
    assert rows[1].endswith("| event 7") and rows[-1].endswith(f"| event {len(lines) - 1}")
    assert all(LINE.match(r) for r in rows[1:])


def test_lines_are_one_line_redacted_and_capped() -> None:
    item = line(
        T0, "cloudwatch_logs", "/ecs/x", "Traceback\n  token ghp_" + "a" * 36 + " " + "x" * 900
    )
    assert "\n" not in item.message and "ghp_" not in item.message
    assert len(item.message) <= 500


def test_timeline_merges_sources_in_time_order_without_duplicates() -> None:
    a = [line(at(2), "ecs", "svc", "b"), line(at(0), "ecs", "svc", "a")]
    b = [line(at(1), "secrets", "s", "changed"), line(at(0), "ecs", "svc", "a")]
    assert [x.message for x in timeline([a, b])] == ["a", "changed", "b"]


# --- collect_evidence over the graph --------------------------------------------------------


class FixtureClients:
    def __init__(self, aws: dict[str, Any], gh: dict[str, Any], calls: list[str]) -> None:
        self._aws, self._gh, self.calls = aws, gh, calls

    def aws(self, connection_id: uuid.UUID) -> AwsReader:
        return _reader(self._aws, self.calls)

    def github(self) -> GitHubApp:
        return github_app(extra_routes=self._gh)

    def installation_id(self, connection_id: uuid.UUID) -> int:
        return INSTALLATION

    def vercel(self, connection_id: uuid.UUID) -> VercelClient:
        raise LookupError("no vercel in this test")

    def kube(self, connection_id: uuid.UUID) -> KubeReader:
        raise LookupError("no cluster in this test")

    def kube_configmaps(self, connection_id: uuid.UUID) -> bool:
        return False


async def _graph(sf: SessionFactory, workspace_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """The checkout demo's api with its neighbors, as discovery stores them."""
    async with sf() as session:
        aws = Connection(workspace_id=workspace_id, kind="aws", name="demo", status="connected")
        gh = Connection(
            workspace_id=workspace_id,
            kind="github",
            name="gh",
            config={"installation_id": 1},
            status="connected",
        )
        vc = Connection(workspace_id=workspace_id, kind="vercel", name="vc", status="connected")
        session.add_all([aws, gh, vc])
        await session.flush()

        def res(type_: str, name: str, external_id: str, **kw: Any) -> Resource:
            r = Resource(
                workspace_id=workspace_id,
                connection_id=kw.pop("conn", aws.id),
                provider=kw.pop("provider", "aws"),
                type=type_,
                name=name,
                external_id=external_id,
                account=ACCOUNT,
                region=REGION,
                **kw,
            )
            session.add(r)
            return r

        api = res(
            "ecs:service",
            "checkout-api",
            f"{CLUSTER}/checkout-api",
            config={"cluster_arn": CLUSTER},
        )
        logs = res("logs:log-group", "/ecs/checkout-api", "lg-api")
        secret = res("secretsmanager:secret", "checkout/database-url", SECRET_ARN)
        db = res("rds:db", "checkout-orders-db", "db")
        repo = res(
            "github:repository",
            REPO,
            f"github:repo:{REPO.lower()}",
            provider="github",
            conn=gh.id,
            repo=REPO,
        )
        web = res(
            "vercel:project",
            "checkout-web",
            "vercel:project:prj",
            provider="vercel",
            conn=vc.id,
            config={"project_id": "prj"},
        )
        bucket = res("s3:bucket", "unrelated", "b")
        await session.flush()
        for to, kind in ((logs, "logs_to"), (secret, "uses"), (db, "reads"), (repo, "defined_in")):
            session.add(Edge(workspace_id=workspace_id, from_id=api.id, to_id=to.id, kind=kind))
        session.add(Edge(workspace_id=workspace_id, from_id=web.id, to_id=api.id, kind="calls"))
        session.add(Edge(workspace_id=workspace_id, from_id=bucket.id, to_id=api.id, kind="uses"))
        await session.commit()
        return {"api": api.id, "secret": secret.id, "web": web.id, "bucket": bucket.id}


async def test_collect_follows_the_graph_to_the_right_readers(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    ids = await _graph(session_factory, workspace.id)
    calls: list[str] = []
    clients = FixtureClients({**env_aws(), **index_rds()}, quiet_github(), calls)
    evidence = await collect(
        session_factory, workspace.id, [ids["api"]], SINCE, UNTIL, clients=clients
    )
    assert {r.name for r in evidence.neighbors} == {
        "/ecs/checkout-api",
        "checkout/database-url",
        "checkout-orders-db",
        REPO,
        "checkout-web",
    }  # an incoming `uses` (bucket -> api) is not followed; an incoming `calls` is
    texts = evidence.texts()
    assert set(texts) == {
        "ecs:checkout-api",
        "cloudwatch_logs:/ecs/checkout-api",
        "secrets:checkout/database-url",
        "rds:checkout-orders-db",
        "github:" + REPO,
        "vercel:checkout-web",
        "timeline",
    }
    assert "reader failed: LookupError: no vercel in this test" in texts["vercel:checkout-web"]
    assert "no commits or workflow runs in the incident window" in texts["github:" + REPO]
    assert "GetSecretValue" not in " ".join(calls)
    # The timeline is every line, oldest first; the secret change comes before the failures.
    rows = texts["timeline"].splitlines()
    stamps = [datetime.fromisoformat(r.split(" | ", 1)[0]) for r in rows]
    assert stamps == sorted(stamps)
    first_change = next(i for i, r in enumerate(rows) if "value last changed" in r)
    first_failure = next(i for i, r in enumerate(rows) if "database unreachable" in r)
    assert first_change < first_failure
    assert [c.kind for c in evidence.changes] == ["secret_change", "deploy"]
    assert evidence.changes[0].resource_id == ids["secret"]


async def test_collect_is_scoped_to_the_workspace(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    ids = await _graph(session_factory, workspace.id)
    other = uuid.uuid4()
    collector = GraphEvidenceCollector(
        session_factory, clients=FixtureClients(oom_aws(), oom_github(), [])
    )
    evidence = await collector.collect(workspace.id, [other], SINCE, UNTIL)
    assert evidence.sources == {} and evidence.notes == ["1 resources not found in this workspace"]
    texts = await collector(uuid.uuid4(), [ids["api"]], SINCE, UNTIL)
    assert texts == {}


async def test_missing_index_evidence_points_at_the_migration_commit(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    ids = await _graph(session_factory, workspace.id)
    clients = FixtureClients({**oom_aws(), **index_logs(), **index_rds()}, index_github(), [])
    texts = await GraphEvidenceCollector(session_factory, clients=clients)(
        workspace.id, [ids["api"]], SINCE, UNTIL
    )
    assert "0002_drop_customer_email_index.py" in texts["github:" + REPO]
    assert "[SPIKE]" in texts["rds:checkout-orders-db"]
    assert "latency summary path=/orders/search" in texts["cloudwatch_logs:/ecs/checkout-api"]


# --- dev endpoint ---------------------------------------------------------------------------


@pytest.fixture
async def carol(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 4001, "carol")


async def _get(sf: SessionFactory, member: Member, path: str, **settings: Any) -> httpx.Response:
    app.dependency_overrides[get_session_factory] = lambda: sf
    app.dependency_overrides[get_settings] = lambda: _settings(**settings)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            return await c.get(path, headers=member.headers)
    finally:
        app.dependency_overrides.clear()


async def test_dev_evidence_endpoint_is_dev_only_and_member_only(
    session_factory: SessionFactory, carol: Member
) -> None:
    path = f"/workspaces/{carol.workspace_id}/evidence?resource={uuid.uuid4()}"
    assert (await _get(session_factory, carol, path, app_env="production")).status_code == 404
    resp = await _get(session_factory, carol, path, app_env="dev")
    assert resp.status_code == 200, resp.text
    assert resp.json()["notes"] == ["1 resources not found in this workspace"]
    other = f"/workspaces/{uuid.uuid4()}/evidence?resource={uuid.uuid4()}"
    assert (await _get(session_factory, carol, other, app_env="dev")).status_code == 404
