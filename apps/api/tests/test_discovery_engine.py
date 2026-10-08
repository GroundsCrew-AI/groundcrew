"""Discovery engine on real Postgres: runs, upsert, stale marking, edges, coverage, the
summary, failures, and the worker job kinds. Connectors return the fixture inventories."""

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.models import (
    Connection,
    Edge,
    Job,
    Resource,
    Run,
    RunStatus,
    RunStep,
    StopReason,
    Workspace,
)
from app.db.session import SessionFactory
from app.discovery.engine import DiscoveryEngine, start_discovery
from app.discovery.types import ConnectorError, ConnectorResult, Progress
from app.worker import Worker, default_handlers
from tests.discovery_helpers import aws_result, github_result, vercel_result
from tests.fakes import FakeLLM, reply, status_error

pytestmark = pytest.mark.anyio

SUMMARY = (
    "- checkout-api and checkout-worker run on ECS\n- Risk: checkout-api has no stable endpoint"
)


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"model_nano": "test/nano", "model_super": "test/super", **overrides}
    return Settings(_env_file=None, **values)


class Inventory:
    """Connector stand-ins returning fixture results, editable between runs."""

    def __init__(self) -> None:
        self.results: dict[str, ConnectorResult | Exception] = {}
        self.calls: list[str] = []

    async def load(self) -> None:
        self.results = {
            "aws": await aws_result(),
            "github": await github_result(),
            "vercel": await vercel_result(),
        }

    def connectors(self) -> dict[str, Any]:
        async def run(conn: Connection, _settings: Settings, progress: Progress) -> ConnectorResult:
            self.calls.append(conn.kind)
            await progress(f"reading {conn.kind}")
            result = self.results[conn.kind]
            if isinstance(result, Exception):
                raise result
            return result.model_copy(deep=True)

        return {"aws": run, "github": run, "vercel": run}


@pytest.fixture
async def inventory() -> Inventory:
    inv = Inventory()
    await inv.load()
    return inv


async def _connections(sf: SessionFactory, ws: Workspace) -> dict[str, uuid.UUID]:
    async with sf() as s:
        conns = [
            Connection(
                workspace_id=ws.id,
                kind="aws",
                name="AWS 881050425883",
                status="connected",
                config={
                    "role_arn": "arn:aws:iam::881050425883:role/GroundcrewReadOnly",
                    "external_id": "gc-x",
                    "account_id": "881050425883",
                },
            ),
            Connection(
                workspace_id=ws.id,
                kind="github",
                name="GitHub GroundsCrew-AI",
                status="connected",
                config={"installation_id": 1, "repos": ["GroundsCrew-AI/checkout-demo"]},
            ),
            Connection(
                workspace_id=ws.id,
                kind="vercel",
                name="Vercel GroundCrew",
                status="connected",
                config={"team_id": "t"},
            ),
            Connection(
                workspace_id=ws.id, kind="azure", name="Azure rg", status="pending", config={}
            ),
        ]
        s.add_all(conns)
        await s.commit()
        return {c.kind: c.id for c in conns}


async def _discover(
    sf: SessionFactory,
    ws: Workspace,
    inv: Inventory,
    llm: FakeLLM | None = None,
    connection_id: uuid.UUID | None = None,
) -> Run:
    async with sf() as s:
        conn = await s.get(Connection, connection_id) if connection_id else None
        run = await start_discovery(s, ws.id, connection=conn)
        await s.commit()
        run_id = run.id
    engine = DiscoveryEngine(
        sf,
        settings=_settings(),
        llm=llm or FakeLLM(reply(SUMMARY), settings=_settings()),
        connectors=inv.connectors(),
    )
    await engine.run(run_id, connection_id=connection_id)
    async with sf() as s:
        done = await s.get(Run, run_id)
        assert done is not None
        return done


async def _resources(sf: SessionFactory, ws: Workspace) -> dict[tuple[str, str], Resource]:
    async with sf() as s:
        rows = await s.scalars(select(Resource).where(Resource.workspace_id == ws.id))
        return {(r.type, r.name): r for r in rows}


async def test_full_discovery_stores_graph_and_summary(
    session_factory: SessionFactory, workspace: Workspace, inventory: Inventory
) -> None:
    ids = await _connections(session_factory, workspace)
    run = await _discover(session_factory, workspace, inventory)
    assert run.status == RunStatus.succeeded and run.stop_reason == StopReason.completed
    assert run.result and "3 connections" in run.result
    assert sorted(inventory.calls) == ["aws", "github", "vercel"]  # Azure (preview) skipped

    resources = await _resources(session_factory, workspace)
    api = resources[("ecs:service", "checkout-api")]
    assert api.connection_id == ids["aws"] and api.account == "881050425883"
    assert api.iac_path == "infra/aws/ecs.tf#L94" and api.repo == "GroundsCrew-AI/checkout-demo"
    assert api.coverage == "diagnosable"
    assert resources[("ecr:repository", "checkout-api")].coverage == "observed"
    repo = resources[("github:repository", "GroundsCrew-AI/checkout-demo")]
    assert "iac_facts" in repo.config  # kept for edge inference on later runs

    async with session_factory() as s:
        edges = list(await s.scalars(select(Edge).where(Edge.workspace_id == workspace.id)))
        names = {r.id: r.name for r in resources.values()}
        found = {(names[e.from_id], e.kind, names[e.to_id]): e for e in edges}
        calls = found[("checkout-demo", "calls", "checkout-api")]
        assert calls.confidence == "medium" and calls.evidence["rule"] == "env_url"
        assert found[("checkout-api", "reads", "checkout-orders-db")].confidence == "high"
        assert (
            "Security group checkout-db"
            in found[("checkout-api", "reads", "checkout-orders-db")].evidence["text"]
        )

        ws = await s.get(Workspace, workspace.id)
        assert ws is not None
        summary = ws.settings["system_summary"]
        assert summary["text"] == SUMMARY and summary["run_id"] == str(run.id)
        assert any("stable endpoint" in r for r in summary["risks"])

        steps = list(
            await s.scalars(select(RunStep).where(RunStep.run_id == run.id).order_by(RunStep.seq))
        )
        tools = {st.tool_name for st in steps if st.kind == "tool"}
        assert {"discover_aws", "discover_github", "discover_vercel", "build_graph"} <= tools
        llm_steps = [st for st in steps if st.kind == "llm"]
        assert len(llm_steps) == 1 and llm_steps[0].model_tier == "nano"
        assert [st.seq for st in steps] == list(range(1, len(steps) + 1))

        aws_conn = await s.get(Connection, ids["aws"])
        assert aws_conn is not None and aws_conn.last_discovery["status"] == "succeeded"
        assert aws_conn.last_discovery["resources"] == len(inventory.results["aws"].resources)  # type: ignore[union-attr]


async def test_unseen_resources_become_stale_and_come_back(
    session_factory: SessionFactory, workspace: Workspace, inventory: Inventory
) -> None:
    await _connections(session_factory, workspace)
    await _discover(session_factory, workspace, inventory)
    aws = inventory.results["aws"]
    assert isinstance(aws, ConnectorResult)
    full = list(aws.resources)
    aws.resources = [r for r in full if r.name != "checkout-worker" or r.type != "ecs:service"]
    await _discover(session_factory, workspace, inventory)
    worker = (await _resources(session_factory, workspace))[("ecs:service", "checkout-worker")]
    assert worker.stale is True
    async with session_factory() as s:
        edge_ids = {
            e.from_id
            for e in await s.scalars(select(Edge).where(Edge.workspace_id == workspace.id))
        }
        assert worker.id not in edge_ids  # stale resources leave the graph
        before = await s.scalar(
            select(Resource.id).where(
                Resource.name == "checkout-api", Resource.type == "ecs:service"
            )
        )
    aws.resources = full
    await _discover(session_factory, workspace, inventory)
    after = await _resources(session_factory, workspace)
    assert after[("ecs:service", "checkout-worker")].stale is False
    assert after[("ecs:service", "checkout-api")].id == before  # upserted in place, same id


async def test_truncated_results_mark_nothing_stale(
    session_factory: SessionFactory, workspace: Workspace, inventory: Inventory
) -> None:
    await _connections(session_factory, workspace)
    await _discover(session_factory, workspace, inventory)
    aws = inventory.results["aws"]
    assert isinstance(aws, ConnectorResult)
    aws.resources, aws.truncated = aws.resources[:2], True
    await _discover(session_factory, workspace, inventory)
    assert not any(r.stale for r in (await _resources(session_factory, workspace)).values())


async def test_one_failing_connection_does_not_fail_the_others(
    session_factory: SessionFactory, workspace: Workspace, inventory: Inventory
) -> None:
    ids = await _connections(session_factory, workspace)
    inventory.results["vercel"] = ConnectorError("Vercel /v9/projects returned 403: forbidden")
    run = await _discover(session_factory, workspace, inventory)
    assert run.status == RunStatus.succeeded and "failed: Vercel GroundCrew" in (run.result or "")
    resources = await _resources(session_factory, workspace)
    assert ("ecs:service", "checkout-api") in resources and not any(
        t.startswith("vercel:") for t, _ in resources
    )
    async with session_factory() as s:
        conn = await s.get(Connection, ids["vercel"])
        assert conn is not None and conn.last_discovery["status"] == "failed"
        assert "403" in conn.last_discovery["error"]
        errors = list(
            await s.scalars(
                select(RunStep).where(RunStep.run_id == run.id, RunStep.kind == "error")
            )
        )
        assert errors and errors[0].tool_name == "discover_vercel"


async def test_summary_failure_keeps_the_graph_and_risks(
    session_factory: SessionFactory, workspace: Workspace, inventory: Inventory
) -> None:
    await _connections(session_factory, workspace)
    llm = FakeLLM(*[status_error(500)] * 12, settings=_settings())
    run = await _discover(session_factory, workspace, inventory, llm=llm)
    assert run.status == RunStatus.succeeded
    async with session_factory() as s:
        ws = await s.get(Workspace, workspace.id)
        assert ws is not None
        summary = ws.settings["system_summary"]
        assert summary["text"] is None and summary["error"] and summary["risks"]


async def test_single_connection_refresh(
    session_factory: SessionFactory, workspace: Workspace, inventory: Inventory
) -> None:
    ids = await _connections(session_factory, workspace)
    run = await _discover(session_factory, workspace, inventory, connection_id=ids["vercel"])
    assert run.status == RunStatus.succeeded and inventory.calls == ["vercel"]


async def test_stopped_run_is_not_run(
    session_factory: SessionFactory, workspace: Workspace, inventory: Inventory
) -> None:
    await _connections(session_factory, workspace)
    async with session_factory() as s:
        run = await start_discovery(s, workspace.id)
        run.status = RunStatus.stopped
        await s.commit()
    await DiscoveryEngine(
        session_factory, settings=_settings(), connectors=inventory.connectors()
    ).run(run.id)
    assert inventory.calls == []


async def test_worker_runs_discovery_jobs(
    session_factory: SessionFactory,
    workspace: Workspace,
    inventory: Inventory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.discovery.engine as engine_module

    handlers = default_handlers()
    assert handlers["discover"] is handlers["discover_workspace"] is engine_module.discovery_job
    monkeypatch.setattr(engine_module, "DEFAULT_CONNECTORS", inventory.connectors())
    monkeypatch.setattr(
        engine_module.DiscoveryEngine,
        "llm",
        lambda self: FakeLLM(reply(SUMMARY), settings=_settings()),
    )
    await _connections(session_factory, workspace)
    async with session_factory() as s:
        run = await start_discovery(s, workspace.id)
        await s.commit()
        job = await s.scalar(select(Job).where(Job.kind == "discover_workspace"))
        assert job is not None and job.max_attempts == 1 and job.payload["run_id"] == str(run.id)
    worker = Worker(session_factory, handlers, poll_interval=0.01)
    assert await worker.run_once()
    async with session_factory() as s:
        done = await s.get(Run, run.id)
        assert done is not None and done.status == RunStatus.succeeded and done.agent == "discovery"
