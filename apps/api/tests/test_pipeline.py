"""The deploy pipeline end to end with fakes: POST /deploys -> worker (Scout, Builder, Deployer)
-> approval -> apply -> verify -> live URL, on the real API, worker, approvals, tokens, and
graph sink, with FakeLLM, a scripted sandbox, and K8sTarget on a fake Kubernetes API."""

import json
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

    def __init__(self, events: list[str], *, failures: int = 1, same_error: bool = False) -> None:
        self.events = events
        self.failures = failures
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
        return SmokeResult(True, [SmokeCheck(health_path or "/", True, 200, "HTTP 200")], 5)

    async def push(self, image_ref: str, target: PushTarget) -> PushResult:
        self.events.append(f"push {target.image}")
        return PushResult(target.image, "sha256:" + "d" * 64, 10)

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
        {"name": "DATABASE_URL", "secret": True, "required": True, "provided": True, "value": None}
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
