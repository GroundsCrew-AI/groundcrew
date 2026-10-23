"""The deploy pipeline end to end with fakes: POST /deploys -> worker (Scout, Builder, Deployer)
-> approval -> apply -> verify -> live URL, on the real API, worker, approvals, tokens, and
graph sink, with FakeLLM, a scripted sandbox, and K8sTarget on a fake Kubernetes API."""

import json
import shutil
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import (
    Approval,
    Connection,
    Edge,
    Job,
    Resource,
    Run,
    RunStatus,
    RunStep,
    Workspace,
)
from app.db.session import SessionFactory, get_session_factory
from app.graph.sink import GraphResourceSink
from app.main import app
from app.pipeline.runner import PipelineDeps, PipelineRunner
from app.sandbox.base import BuildResult, PushResult, PushTarget, SmokeCheck, SmokeEnv, SmokeResult
from app.security.crypto import generate_key
from app.targets import K8sTarget
from app.worker import Worker
from tests.fakes import FakeLLM, reply

from .target_fakes import FakeKubeApi
from .test_api import Member, _member, _settings

pytestmark = pytest.mark.anyio

SECRET = "postgresql://app:hunter2@db:5432/app"  # noqa: S105 (test value)
LOGS = Path(__file__).parent / "fixtures" / "sandbox"
PSYCOPG2 = (LOGS / "psycopg2-build.log").read_text()
NAIVE = "FROM python:3.12-slim\nRUN pip install -r requirements.txt\nCMD gunicorn app:app\n"
FIXED = "FROM python:3.12-slim\nRUN apt-get install -y libpq-dev gcc\n" + NAIVE.split("\n", 1)[1]


class Sandbox:
    """Builds fail once on psycopg2, then pass; every call is logged in `events`."""

    def __init__(
        self,
        events: list[str],
        *,
        failures: int = 1,
        same_error: bool = False,
        smoke: SmokeResult | None = None,
    ) -> None:
        self.events = events
        self.failures = failures
        self.smoke = smoke
        self.builds = 0

    async def build(
        self, context_dir: Path, dockerfile: Path, tag: str, *, run_id: str
    ) -> BuildResult:
        self.builds += 1
        self.events.append(f"build {self.builds}")
        if self.builds <= self.failures:
            from app.sandbox.logs import error_lines, failing_step

            return BuildResult(
                False, PSYCOPG2, None, 1000, failing_step(PSYCOPG2), error_lines(PSYCOPG2)
            )
        return BuildResult(True, "", tag, 2000)

    async def run_smoke(
        self,
        image_ref: str,
        port: int,
        env: list[SmokeEnv],
        *,
        run_id: str,
        health_path: str | None = None,
    ) -> SmokeResult:
        self.events.append("smoke " + ",".join(sorted(e.name for e in env)))
        if self.smoke is not None:
            return self.smoke
        return SmokeResult(True, [SmokeCheck(health_path or "/", True, 200, "HTTP 200")], 5)

    async def push(self, image_ref: str, target: PushTarget) -> PushResult:
        self.events.append(f"push {target.image}")
        return PushResult(target.image, "sha256:" + "d" * 64, 10)

    async def push_to_groundcrew(
        self, image_ref: str, registry_url: str, repository: str, tag: str
    ) -> PushResult:
        image = f"{registry_url}/{repository}:{tag}"
        self.events.append(f"push {image}")
        return PushResult(image, "sha256:" + "d" * 64, 10)

    async def image_id(self, image_ref: str) -> str:
        return "sha256:" + "e" * 64

    async def cleanup(self, run_id: str) -> None:
        self.events.append("cleanup")


class NoCredentials:
    def close(self) -> None:
        return None


def _health_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))


async def no_sleep(_s: float) -> None:
    return None


def _llm_replies(*, missing_env: bool = False) -> list[Any]:
    return [
        reply("{}"),  # Scout: rules only
        reply(json.dumps({"dockerfile": NAIVE, "rationale": "slim"})),
        reply(
            json.dumps(
                {
                    "dockerfile": FIXED,
                    "rationale": "pg headers",
                    "changed_lines": ["RUN apt-get"],
                    "fix_kind": "system_packages",
                }
            )
        ),
        reply(
            json.dumps(
                {
                    "replicas": 1,
                    "size": "small",
                    "secret_env": ["DATABASE_URL"],
                    "rationale": "demo",
                }
            )
        ),
    ]


class Stack:
    def __init__(self, sf: SessionFactory, member: Member, **sandbox_kw: Any) -> None:
        self.sf = sf
        self.member = member
        self.events: list[str] = []
        self.kube = FakeKubeApi(self.events)
        self.sandbox = Sandbox(self.events, **sandbox_kw)
        self.settings = _settings(
            model_super="test/super",
            app_env="dev",
            gc_kind_registry="kind-registry:5000",
            gc_verify_timeout_seconds=30,
        )
        self.llm = FakeLLM(*_llm_replies(), settings=self.settings)
        self.http = _health_client()

        def target_for(settings: Any, credentials: Any, conn: Connection) -> K8sTarget:
            return K8sTarget(
                settings,
                credentials,
                conn.id,
                cluster="kind-test",
                api=self.kube,
                http=self.http,
                sleep=no_sleep,
                session_factory=sf,
            )

        async def creds(_session: Any, _ws: uuid.UUID) -> Any:
            return NoCredentials()

        self.deps = PipelineDeps(
            settings=self.settings,
            llm=self.llm,
            sandbox=self.sandbox,
            sink=GraphResourceSink(sf),
            target_for=target_for,
            credentials_for=creds,
            builder_sleep=no_sleep,
        )

        async def run(session_factory: SessionFactory, payload: dict[str, Any]) -> None:
            await PipelineRunner(session_factory, self.deps).run(uuid.UUID(payload["run_id"]))

        self.worker = Worker(sf, {"deploy": run, "resume_run": run}, poll_interval=0)

    async def drain(self) -> None:
        while await self.worker.run_once():
            pass


@pytest.fixture(autouse=True)
def secrets_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Env values are Fernet-encrypted in the job payload."""
    monkeypatch.setenv("SECRETS_KEY", generate_key())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def alice(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 1001, "alice")


@pytest.fixture
async def client(session_factory: SessionFactory) -> AsyncIterator[httpx.AsyncClient]:
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = lambda: _settings(app_env="dev")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _connection(
    sf: SessionFactory, ws: uuid.UUID, kind: str, name: str, config: dict[str, Any]
) -> uuid.UUID:
    async with sf() as session:
        conn = Connection(workspace_id=ws, kind=kind, name=name, config=config, status="connected")
        session.add(conn)
        await session.commit()
        return conn.id


async def _start(
    client: httpx.AsyncClient, member: Member, target: uuid.UUID, env: list[dict[str, Any]]
) -> str:
    r = await client.post(
        "/deploys",
        json={
            "workspace_id": str(member.workspace_id),
            "repo": {"fixture": "py-build-trap"},
            "target_connection_id": str(target),
            "env": env,
        },
        headers=member.headers,
    )
    assert r.status_code == 202, r.text
    assert r.json()["deploy_id"] == r.json()["run_id"]
    return str(r.json()["deploy_id"])


async def _run(sf: SessionFactory, run_id: str) -> Run:
    async with sf() as session:
        run = await session.get(Run, uuid.UUID(run_id))
        assert run is not None
        return run


async def _steps(
    sf: SessionFactory, run_id: str
) -> list[Any]:  # RunStep rows; `data` is a dict on deploy events
    async with sf() as session:
        return list(
            await session.scalars(
                select(RunStep).where(RunStep.run_id == uuid.UUID(run_id)).order_by(RunStep.seq)
            )
        )


async def _everything_stored(sf: SessionFactory, run_id: str) -> str:
    """Every place a secret must never be: run, steps, approvals (jobs checked separately)."""
    run = await _run(sf, run_id)
    async with sf() as session:
        approvals = list(await session.scalars(select(Approval).where(Approval.run_id == run.id)))
    steps = await _steps(sf, run_id)
    return json.dumps(
        [
            run.state,
            run.error,
            run.result,
            [(s.input_summary, s.output_summary, s.data) for s in steps],
            [a.arguments for a in approvals],
        ],
        default=str,
    )


async def test_kind_deploy_from_post_to_live_url(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    stack = Stack(session_factory, alice)
    k8s = await _connection(
        session_factory, alice.workspace_id, "k8s", "kind groundcrew", {"cluster": "kind-test"}
    )
    deploy_id = await _start(
        client, alice, k8s, [{"name": "DATABASE_URL", "value": SECRET, "secret": True}]
    )
    async with session_factory() as session:
        job = await session.scalar(select(Job).where(Job.kind == "deploy"))
        assert job is not None and "hunter2" not in json.dumps(job.payload)
    await stack.drain()

    run = await _run(session_factory, deploy_id)
    assert run.status == RunStatus.waiting_approval
    steps = await _steps(session_factory, deploy_id)
    kinds = [s.kind for s in steps]
    attempts = [s.data for s in steps if s.kind == "build_attempt"]
    assert [a["ok"] for a in attempts] == [False, True] and attempts[1][
        "dockerfile_source"
    ] == "fixed"
    assert kinds.count("llm") == 4 and "approval_requested" in kinds
    started = [s.data["stage"] for s in steps if s.kind == "stage_started"]
    assert started == ["scout", "build", "smoke", "plan", "preflight", "approval"]
    # Built and smoke-tested, nothing pushed before approval.
    assert stack.events == ["build 1", "build 2", "smoke DATABASE_URL"]
    assert "hunter2" not in await _everything_stored(session_factory, deploy_id)

    listed = (
        await client.get(f"/deploys?workspace_id={alice.workspace_id}", headers=alice.headers)
    ).json()
    item = listed["items"][0]
    assert (
        item["status"] == "waiting_approval"
        and item["build_attempts"] == 2
        and item["automatic_fix"]
    )
    assert item["stage"] == "approval" and item["app"] == "py-build-trap"
    detail = (await client.get(f"/deploys/{deploy_id}", headers=alice.headers)).json()
    assert detail["plan"]["resources"][0]["action"] == "push"
    assert detail["plan"]["source_image"] == "sha256:" + "e" * 64
    assert detail["env"] == [
        {
            "name": "DATABASE_URL",
            "secret": True,
            "required": True,
            "provided": True,
            "value": None,
            "change": None,
        }
    ]

    approval_id = next(s.approval_id for s in steps if s.kind == "approval_requested")
    r = await client.post(
        f"/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=alice.headers
    )
    assert r.status_code == 200, r.text
    await stack.drain()

    run = await _run(session_factory, deploy_id)
    assert run.status == RunStatus.succeeded, run.error
    steps = await _steps(session_factory, deploy_id)
    live = next(s.data for s in steps if s.kind == "live_url")
    assert live["url"] == "http://py-build-trap.localhost" and live["resource_id"]
    finished = next(s.data for s in steps if s.kind == "run_finished")
    assert finished["automatic_fix"] and finished["build_attempts"] == 2 and finished["live_url"]
    # The push came after approval, first in apply.
    assert stack.events.index("push kind-registry:5000/py-build-trap:" + run.id.hex[:12]) == 3
    assert stack.events[-1] == "cleanup"
    detail = (await client.get(f"/deploys/{deploy_id}", headers=alice.headers)).json()
    assert detail["status"] == "live" and detail["live_url"] == "http://py-build-trap.localhost"
    assert (
        detail["deploy"]["status"] == "live" and detail["deploy"]["repo"] == "fixture:py-build-trap"
    )
    # Registered exactly once, and actionable.
    async with session_factory() as session:
        rows = list(
            await session.scalars(
                select(Resource).where(
                    Resource.workspace_id == alice.workspace_id, Resource.type == "k8s:deployment"
                )
            )
        )
        assert (
            len(rows) == 1
            and rows[0].coverage == "actionable"
            and str(rows[0].id) == live["resource_id"]
        )
        edges = await session.scalar(select(func.count(Edge.id)).where(Edge.to_id == rows[0].id))
        edges = (edges or 0) + (
            await session.scalar(select(func.count(Edge.id)).where(Edge.from_id == rows[0].id)) or 0
        )
        assert edges == 1
        jobs = list(await session.scalars(select(Job)))
        assert all("env" not in (j.payload or {}) for j in jobs)
        token_approval = await session.get(Approval, approval_id)
        assert token_approval is not None and token_approval.used_at is not None
    assert "hunter2" not in await _everything_stored(session_factory, deploy_id)


async def test_a_rejected_plan_changes_nothing(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    stack = Stack(session_factory, alice)
    k8s = await _connection(
        session_factory, alice.workspace_id, "k8s", "kind", {"cluster": "kind-test"}
    )
    deploy_id = await _start(
        client, alice, k8s, [{"name": "DATABASE_URL", "value": SECRET, "secret": True}]
    )
    await stack.drain()
    approval_id = next(
        s.approval_id
        for s in await _steps(session_factory, deploy_id)
        if s.kind == "approval_requested"
    )
    await client.post(
        f"/approvals/{approval_id}/decide", json={"decision": "reject"}, headers=alice.headers
    )
    await stack.drain()
    run = await _run(session_factory, deploy_id)
    assert run.status == RunStatus.stopped and "rejected" in (run.error or "")
    assert not any(e.startswith("push") for e in stack.events) and stack.kube.objects == {}
    detail = (await client.get(f"/deploys/{deploy_id}", headers=alice.headers)).json()
    assert detail["status"] == "stopped"
    async with session_factory() as session:
        assert all("env" not in (j.payload or {}) for j in await session.scalars(select(Job)))


async def test_missing_env_is_set_at_the_approval_card_without_a_rebuild(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    stack = Stack(session_factory, alice)
    k8s = await _connection(
        session_factory, alice.workspace_id, "k8s", "kind", {"cluster": "kind-test"}
    )
    deploy_id = await _start(client, alice, k8s, [])
    await stack.drain()
    steps = await _steps(session_factory, deploy_id)
    plan = next(s for s in steps if s.kind == "approval_requested")
    env_check = next(c for c in plan.data["preflight"] if c["name"] == "env")
    assert env_check["status"] == "fail"
    builds, llm_calls = stack.sandbox.builds, len(stack.llm.calls)

    r = await client.post(
        f"/deploys/{deploy_id}/env",
        json={
            "approval_id": str(plan.approval_id),
            "env": [{"name": "DATABASE_URL", "value": SECRET, "secret": True}],
        },
        headers=alice.headers,
    )
    assert r.status_code == 202, r.text and "hunter2" not in r.text
    again = await client.post(
        f"/deploys/{deploy_id}/env",
        json={"approval_id": str(plan.approval_id)},
        headers=alice.headers,
    )
    assert again.status_code == 409
    await stack.drain()

    run = await _run(session_factory, deploy_id)
    assert run.status == RunStatus.waiting_approval
    steps = await _steps(session_factory, deploy_id)
    plans = [s for s in steps if s.kind == "approval_requested"]
    assert len(plans) == 2
    new_check = next(c for c in plans[1].data["preflight"] if c["name"] == "env")
    assert new_check["status"] == "pass"
    assert plans[1].data["source_image"] == plans[0].data["source_image"]
    assert stack.sandbox.builds == builds and len(stack.llm.calls) == llm_calls
    replaced = [
        s.data for s in steps if s.kind == "stage_finished" and s.data["stage"] == "approval"
    ]
    assert replaced[0]["status"] == "skipped" and "DATABASE_URL set" in replaced[0]["summary"]
    assert "hunter2" not in await _everything_stored(session_factory, deploy_id)

    await client.post(
        f"/approvals/{plans[1].approval_id}/decide",
        json={"decision": "approve"},
        headers=alice.headers,
    )
    await stack.drain()
    run = await _run(session_factory, deploy_id)
    assert run.status == RunStatus.succeeded, run.error
    secret = stack.kube.objects[("secrets", "gc-py-build-trap", "py-build-trap-env")]
    assert secret["stringData"] == {"DATABASE_URL": SECRET}


async def test_a_failed_verify_offers_a_rollback_that_needs_an_emergency_approval(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    stack = Stack(session_factory, alice)
    stack.kube.ready = False
    stack.kube.pods = [
        {
            "metadata": {"name": "p"},
            "status": {
                "containerStatuses": [
                    {"state": {"waiting": {"reason": "CrashLoopBackOff"}}, "restartCount": 3}
                ]
            },
        }
    ]
    k8s = await _connection(
        session_factory, alice.workspace_id, "k8s", "kind", {"cluster": "kind-test"}
    )
    deploy_id = await _start(
        client, alice, k8s, [{"name": "DATABASE_URL", "value": SECRET, "secret": True}]
    )
    await stack.drain()
    approval_id = next(
        s.approval_id
        for s in await _steps(session_factory, deploy_id)
        if s.kind == "approval_requested"
    )
    await client.post(
        f"/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=alice.headers
    )
    await stack.drain()

    run = await _run(session_factory, deploy_id)
    assert run.status == RunStatus.waiting_approval
    steps = await _steps(session_factory, deploy_id)
    verify = next(s.data for s in steps if s.kind == "verify_result")
    assert not verify["ok"] and verify["logs_tail"]
    offered = next(s.data for s in steps if s.kind == "rollback_offered")
    assert offered["to_revision"] is None
    async with session_factory() as session:
        rollback = await session.get(Approval, uuid.UUID(offered["approval_id"]))
        assert (
            rollback is not None
            and rollback.policy == "emergency"
            and rollback.tool_name == "target_rollback"
        )
    assert ("namespaces", None, "gc-py-build-trap") in stack.kube.objects

    await client.post(
        f"/approvals/{offered['approval_id']}/decide",
        json={"decision": "approve"},
        headers=alice.headers,
    )
    await stack.drain()
    run = await _run(session_factory, deploy_id)
    assert run.status == RunStatus.failed
    assert ("namespaces", None, "gc-py-build-trap") not in stack.kube.objects
    detail = (await client.get(f"/deploys/{deploy_id}", headers=alice.headers)).json()
    assert detail["status"] == "rolled_back" and detail["deploy"]["status"] == "rolled_back"


async def test_no_progress_stops_the_run(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    stack = Stack(session_factory, alice, failures=5)
    stack.llm = FakeLLM(
        reply("{}"),
        reply(json.dumps({"dockerfile": NAIVE, "rationale": "slim"})),
        *(
            reply(
                json.dumps(
                    {
                        "dockerfile": FIXED + f"# {i}\n",
                        "rationale": "x",
                        "changed_lines": [],
                        "fix_kind": "system_packages",
                    }
                )
            )
            for i in range(3)
        ),
        settings=stack.settings,
    )
    stack.deps.llm = stack.llm
    k8s = await _connection(
        session_factory, alice.workspace_id, "k8s", "kind", {"cluster": "kind-test"}
    )
    deploy_id = await _start(client, alice, k8s, [])
    await stack.drain()
    run = await _run(session_factory, deploy_id)
    assert run.status == RunStatus.failed and run.stop_reason == "no_progress"
    detail = (await client.get(f"/deploys/{deploy_id}", headers=alice.headers)).json()
    assert detail["status"] == "failed" and detail["build_attempts"] == 3


async def test_create_checks_the_source_and_target(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    observe = await _connection(
        session_factory, alice.workspace_id, "aws", "AWS 1", {"tier": "observe"}
    )
    r = await client.post(
        "/deploys",
        json={
            "workspace_id": str(alice.workspace_id),
            "repo": {"fixture": "py-build-trap"},
            "target_connection_id": str(observe),
        },
        headers=alice.headers,
    )
    assert r.status_code == 409 and "Turn on deploys" in r.text
    vercel = await _connection(session_factory, alice.workspace_id, "vercel", "Vercel", {})
    r = await client.post(
        "/deploys",
        json={
            "workspace_id": str(alice.workspace_id),
            "repo": {
                "connection_id": str(vercel),
                "repo": "GroundsCrew-AI/legacy-flask-app",
                "ref": "main",
            },
            "target_connection_id": str(observe),
        },
        headers=alice.headers,
    )
    assert r.status_code == 409 and "not GitHub" in r.text
    app.dependency_overrides[get_settings] = lambda: _settings(app_env="production")
    k8s = await _connection(session_factory, alice.workspace_id, "k8s", "kind", {})
    r = await client.post(
        "/deploys",
        json={
            "workspace_id": str(alice.workspace_id),
            "repo": {"fixture": "py-build-trap"},
            "target_connection_id": str(k8s),
        },
        headers=alice.headers,
    )
    assert r.status_code == 409 and "local dev" in r.text
    r = await client.get(f"/deploys/{uuid.uuid4()}", headers=alice.headers)
    assert r.status_code == 404


def test_fixtures_come_from_the_setting_or_the_checkout(tmp_path: Path) -> None:
    from app.pipeline.runner import fixtures_dir

    assert fixtures_dir(_settings(deploy_fixtures_dir=str(tmp_path))) == tmp_path
    assert (fixtures_dir(_settings()) / "py-build-trap" / "repo").is_dir()


async def test_an_unexpected_error_fails_the_running_stage(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    from app.targets import TargetError

    stack = Stack(session_factory, alice)

    real_target_for = stack.deps.target_for

    def unreachable(*args: Any) -> Any:
        target = real_target_for(*args)

        async def plan(_spec: Any) -> Any:
            raise TargetError("Cannot resolve the cluster address https://x:6443.")

        target.plan = plan  # type: ignore[method-assign,assignment]
        return target

    stack.deps.target_for = unreachable
    k8s = await _connection(session_factory, alice.workspace_id, "k8s", "kind", {"cluster": "c"})
    deploy_id = await _start(client, alice, k8s, [])
    await stack.drain()
    run = await _run(session_factory, deploy_id)
    assert run.status == RunStatus.failed and "Cannot resolve" in (run.error or "")
    detail = (await client.get(f"/deploys/{deploy_id}", headers=alice.headers)).json()
    stages = {s["stage"]: s["status"] for s in detail["stages"]}
    assert stages["build"] == "succeeded" and stages["plan"] == "failed"
    assert "running" not in stages.values()


def test_an_unresolvable_cluster_says_how_to_fix_it() -> None:
    from app.targets.k8s import unreachable

    message = unreachable(
        "https://groundcrew-control-plane:6443", OSError("NameResolutionError: x")
    )
    assert "make dev KIND=1" in message


async def test_a_workspace_out_of_builds_stops_before_building(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    stack = Stack(session_factory, alice)
    async with session_factory() as session:
        ws = await session.get_one(Workspace, alice.workspace_id)
        ws.settings = {**(ws.settings or {}), "quotas": {"builds": 0}}
        await session.commit()
    k8s = await _connection(
        session_factory, alice.workspace_id, "k8s", "kind", {"cluster": "kind-test"}
    )
    deploy_id = await _start(client, alice, k8s, [])
    await stack.drain()
    run = await _run(session_factory, deploy_id)
    assert run.status == RunStatus.failed and run.stop_reason == "budget"
    assert "limit of 0 deploy builds today" in (run.error or "")
    assert not [e for e in stack.events if e.startswith("build")]


async def test_an_app_that_answers_but_is_unhealthy_in_the_sandbox_goes_on(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    """Its health check needs the database, which the sandbox cannot reach: the image is fine,
    the run says why the check failed, and Verify checks health on the target."""
    unhealthy = SmokeResult(
        False,
        [SmokeCheck("/health", False, 503, "HTTP 503")],
        60_000,
        logs_tail='{"msg": "health check failed: database unreachable"}',
        detail="/health did not answer 2xx/3xx within 60s (HTTP 503)",
    )
    stack = Stack(session_factory, alice, smoke=unhealthy)
    k8s = await _connection(
        session_factory, alice.workspace_id, "k8s", "kind", {"cluster": "kind-test"}
    )
    deploy_id = await _start(client, alice, k8s, [])
    await stack.drain()
    run = await _run(session_factory, deploy_id)
    assert run.status == RunStatus.waiting_approval
    steps = await _steps(session_factory, deploy_id)
    assert [s.data["ok"] for s in steps if s.kind == "build_attempt"] == [False, True]
    notes = [s.output_summary or "" for s in steps if s.kind == "note"]
    assert any("cannot reach your databases" in n and "database unreachable" in n for n in notes)


class _MonorepoSource:
    """py-build-trap as the api/ service of a repo that also has a web/ front end."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.commit: str | None = None

    def materialize(self) -> Path:
        fixture = Path(__file__).parents[3] / "demo" / "fixtures" / "py-build-trap" / "repo"
        shutil.copytree(fixture, self.root / "api")
        (self.root / "web").mkdir()
        (self.root / "web" / "package.json").write_text('{"dependencies": {"next": "15"}}')
        (self.root / "web" / "page.ts").write_text("const url = process.env.WEB_ONLY_URL;\n")
        return self.root

    def describe(self) -> str:
        return "monorepo"

    def cleanup(self) -> None:
        return None


async def _start_monorepo(
    client: httpx.AsyncClient, member: Member, target: uuid.UUID, path: str | None
) -> str:
    repo: dict[str, Any] = {"fixture": "py-build-trap"}
    if path:
        repo["path"] = path
    r = await client.post(
        "/deploys",
        json={
            "workspace_id": str(member.workspace_id),
            "repo": repo,
            "target_connection_id": str(target),
            "env": [{"name": "DATABASE_URL", "value": SECRET, "secret": True}],
        },
        headers=member.headers,
    )
    assert r.status_code == 202, r.text
    return str(r.json()["deploy_id"])


async def test_a_repo_with_several_services_asks_which_one(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory, tmp_path: Path
) -> None:
    stack = Stack(session_factory, alice)
    stack.deps.source_for = lambda _request, _conn: _MonorepoSource(tmp_path / "repo")
    k8s = await _connection(session_factory, alice.workspace_id, "k8s", "kind", {"cluster": "k"})
    deploy_id = await _start_monorepo(client, alice, k8s, None)
    await stack.drain()
    run = await _run(session_factory, deploy_id)
    assert run.status == RunStatus.failed
    assert "several services (api, web)" in (run.error or "")
    # Nothing was built, and the model was never asked.
    assert not any(e.startswith("build") for e in stack.sandbox.events)


async def test_only_the_chosen_service_is_read_and_built(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory, tmp_path: Path
) -> None:
    stack = Stack(session_factory, alice)
    stack.deps.source_for = lambda _request, _conn: _MonorepoSource(tmp_path / "repo")
    k8s = await _connection(session_factory, alice.workspace_id, "k8s", "kind", {"cluster": "k"})
    deploy_id = await _start_monorepo(client, alice, k8s, "api")
    await stack.drain()
    run = await _run(session_factory, deploy_id)
    assert run.status == RunStatus.waiting_approval, run.error
    spec = run.state["spec"]
    assert spec["subpath"] == "api"
    # The front end's env var belongs to another service.
    assert "WEB_ONLY_URL" not in {v["name"] for v in spec["env_vars"]}
    assert run.state["app"] == "py-build-trap-api"
    detail = (await client.get(f"/deploys/{deploy_id}", headers=alice.headers)).json()
    assert detail["source"]["path"] == "api"


async def test_settings_change_reuses_the_image_and_keeps_untouched_values(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    stack = Stack(session_factory, alice)
    k8s = await _connection(session_factory, alice.workspace_id, "k8s", "kind", {"cluster": "k"})
    deploy_id = await _start(
        client,
        alice,
        k8s,
        [
            {"name": "DATABASE_URL", "value": SECRET, "secret": True},
            {"name": "LOG_LEVEL", "value": "info"},
            {"name": "FEATURE", "value": "on"},
        ],
    )
    await stack.drain()
    first = next(
        s for s in await _steps(session_factory, deploy_id) if s.kind == "approval_requested"
    )
    await client.post(
        f"/approvals/{first.approval_id}/decide",
        json={"decision": "approve"},
        headers=alice.headers,
    )
    await stack.drain()
    assert (await _run(session_factory, deploy_id)).status == RunStatus.succeeded

    settings = (await client.get(f"/deploys/{deploy_id}/settings", headers=alice.headers)).json()
    assert settings == {
        "env": [
            {"name": "DATABASE_URL", "secret": True},
            {"name": "FEATURE", "secret": False},
            {"name": "LOG_LEVEL", "secret": False},
        ],
        "blocked": None,
    }
    # Nothing to change, or removing a name the app does not have, is refused.
    bad = await client.post(f"/deploys/{deploy_id}/settings", json={}, headers=alice.headers)
    assert bad.status_code == 422
    bad = await client.post(
        f"/deploys/{deploy_id}/settings", json={"remove": ["NOPE"]}, headers=alice.headers
    )
    assert bad.status_code == 422 and "NOPE" in bad.text

    events, builds, llm_calls = len(stack.events), stack.sandbox.builds, len(stack.llm.calls)
    r = await client.post(
        f"/deploys/{deploy_id}/settings",
        json={
            "env": [
                {"name": "LOG_LEVEL", "value": "debug"},
                {"name": "API_TOKEN", "value": "t0ken-value", "secret": True},
            ],
            "remove": ["FEATURE"],
        },
        headers=alice.headers,
    )
    assert r.status_code == 202, r.text
    change_id = r.json()["deploy_id"]
    await stack.drain()

    steps = await _steps(session_factory, change_id)
    skipped = [
        s.data["stage"]
        for s in steps
        if s.kind == "stage_finished" and s.data["status"] == "skipped"
    ]
    assert skipped == ["scout", "build", "smoke"]
    # No model, no build, no smoke test, no push.
    assert len(stack.llm.calls) == llm_calls and stack.sandbox.builds == builds
    assert not any(e.startswith(("push", "smoke", "build")) for e in stack.events[events:])
    detail = (await client.get(f"/deploys/{change_id}", headers=alice.headers)).json()
    assert detail["plan"]["resources"][0]["action"] == "unchanged"
    # A settings change, with none of the live deploy's build attempts.
    assert detail["kind"] == "settings" and detail["build_attempts"] == 0
    assert not detail["automatic_fix"]
    changes = {v["name"]: v["change"] for v in detail["env"]}
    assert changes == {
        "API_TOKEN": "add",
        "DATABASE_URL": "keep",
        "FEATURE": "remove",
        "LOG_LEVEL": "update",
    }
    plan = next(s for s in steps if s.kind == "approval_requested")
    image = next(c for c in plan.data["preflight"] if c["name"] == "image")
    assert "nothing is built or pushed" in image["detail"]
    assert "change the settings" in (plan.output_summary or "")
    await client.post(
        f"/approvals/{plan.approval_id}/decide", json={"decision": "approve"}, headers=alice.headers
    )
    await stack.drain()
    run = await _run(session_factory, change_id)
    assert run.status == RunStatus.succeeded, run.error
    ns = "gc-py-build-trap"
    secret = stack.kube.objects[("secrets", ns, "py-build-trap-env")]
    assert secret["stringData"] == {"API_TOKEN": "t0ken-value", "DATABASE_URL": SECRET}
    deployment = stack.kube.objects[("deployments", ns, "py-build-trap")]
    env = {e["name"]: e for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["LOG_LEVEL"]["value"] == "debug" and "FEATURE" not in env
    assert env["DATABASE_URL"]["valueFrom"]["secretKeyRef"]["key"] == "DATABASE_URL"
    # The first deploy was replaced: its settings are not the ones to change any more.
    old = (await client.get(f"/deploys/{deploy_id}/settings", headers=alice.headers)).json()
    assert old["blocked"] and "newer deploy" in old["blocked"]
    assert "t0ken-value" not in await _everything_stored(session_factory, change_id)


async def test_an_assistant_changes_settings_only_through_an_approval(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    """ "Change LOG_LEVEL to debug on my web deploy" from a 'Read and start work' token: the
    assistant gets a link, and the target keeps the old value until a person approves."""
    from app.mcp_server import endpoint
    from tests.test_mcp import _call, _token

    stack = Stack(session_factory, alice)
    k8s = await _connection(session_factory, alice.workspace_id, "k8s", "kind", {"cluster": "k"})
    deploy_id = await _start(client, alice, k8s, [{"name": "LOG_LEVEL", "value": "info"}])
    await stack.drain()
    first = next(
        s for s in await _steps(session_factory, deploy_id) if s.kind == "approval_requested"
    )
    await client.post(
        f"/approvals/{first.approval_id}/decide",
        json={"decision": "approve"},
        headers=alice.headers,
    )
    await stack.drain()
    ns = "gc-py-build-trap"

    def live_log_level() -> str:
        deployment = stack.kube.objects[("deployments", ns, "py-build-trap")]
        env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        return str(next(e["value"] for e in env if e["name"] == "LOG_LEVEL"))

    assert live_log_level() == "info"

    endpoint.session_factory = session_factory
    try:
        async with endpoint.running():
            reader = (await _token(client, alice))["token"]
            refused = await _call(
                client,
                reader,
                "change_settings",
                deploy_id=deploy_id,
                set_values={"LOG_LEVEL": "debug"},
            )
            assert "can only read" in refused["data"]["error"]

            worker = (await _token(client, alice, role="approver"))["token"]
            deploys = await _call(client, worker, "list_deploys")
            assert deploy_id in json.dumps(deploys["data"])
            changed = await _call(
                client,
                worker,
                "change_settings",
                deploy_id=deploy_id,
                set_values={"LOG_LEVEL": "debug"},
            )
    finally:
        endpoint.session_factory = None
    change_id = changed["data"]["deploy_id"]
    assert changed["data"]["link"].endswith(f"/deploy/{change_id}")

    # Planned, then waiting for a person: the target still runs with info.
    await stack.drain()
    run = await _run(session_factory, change_id)
    assert run.status == RunStatus.waiting_approval, run.error
    assert live_log_level() == "info"
    detail = (await client.get(f"/deploys/{change_id}", headers=alice.headers)).json()
    assert {v["name"]: v["change"] for v in detail["env"]}["LOG_LEVEL"] == "update"

    plan = next(
        s for s in await _steps(session_factory, change_id) if s.kind == "approval_requested"
    )
    await client.post(
        f"/approvals/{plan.approval_id}/decide", json={"decision": "approve"}, headers=alice.headers
    )
    await stack.drain()
    assert (await _run(session_factory, change_id)).status == RunStatus.succeeded
    assert live_log_level() == "debug"
