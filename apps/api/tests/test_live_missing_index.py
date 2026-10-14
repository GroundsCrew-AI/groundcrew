"""The live missing-index run (2026-10-13), replayed on fakes.

A seed-large load test spiked the database 24 minutes before commit b11275e dropped
orders.customer_email; its incident failed and was merged into the real one. The model named
the deploy by its run id ("38384663137", all digits), which Medic read as a SHA prefix and so
found no commit: "No fix PR: the commit or its repository is not connected". Research searched
the health-check line though the app's errors were SQLAlchemy/asyncpg timeouts on
/orders/search, and "Detected in 24m 1s" was timed from the load test's signal.
"""

import json
import uuid
from datetime import timedelta
from typing import Any

import pytest

from app.actions.fake import FakeEmergencyActions
from app.agents.medic import fixes as fx
from app.agents.medic.events import ChangeMarker, Signal
from app.agents.medic.jobs import investigate_incident_job
from app.agents.medic.schemas import RootCause, TriggeringChange
from app.db.models import Connection, Incident, Resource, Run, Workspace
from app.db.session import SessionFactory
from app.incidents.store import open_incident, set_status
from app.llm.fake import FakeLLM, reply
from app.tools.tavily import TavilyClient, dominant_error
from tests.discovery_helpers import INSTALLATION, Recorded, github_app, mock_transport
from tests.test_medic import (
    REPO,
    Harness,
    _evidence,
    _now,
    _state,
    demo_graph,
    eid,
    github_routes,
    settings,
)

pytestmark = pytest.mark.anyio

SHA = "b11275e4c0ffee0123456789abcdef0123456789"
RUN_ID = "38384663137"
DROPPED = "api/migrations/versions/0004_drop_customer_email_index.py"
MIGRATION = "api/migrations/versions/0005_recreate_customer_email_index.py"
ROOT_QUERY = "postgres missing index full table scan"


def _app_error(n: int) -> str:
    return json.dumps(
        {
            "ts": f"2026-10-13T10:39:{10 + n:02d}Z",
            "level": "error",
            "msg": "order search failed",
            "path": "/orders/search",
            "request_id": f"{n:08x}deadbeef",
            "error": "Traceback (most recent call last):\n  ...\n"
            "sqlalchemy.exc.DBAPIError: (sqlalchemy.dialects.postgresql.asyncpg.Error) "
            "<class 'asyncpg.exceptions.QueryCanceledError'>: canceling statement due to "
            "statement timeout",
        }
    )


@pytest.fixture
def use_deps() -> Any:
    from app.agents.medic import agent

    def install(h: Harness) -> None:
        agent.set_deps_factory(lambda sf: h.deps)

    yield install
    agent.set_deps_factory(None)


@pytest.mark.parametrize("signal_minutes", [1.5, 5.0])
async def test_live_missing_index_opens_the_migration_pr_through_the_installation(
    session_factory: SessionFactory, workspace: Workspace, use_deps: Any, signal_minutes: float
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    t0 = _now()
    change_at = t0 - timedelta(minutes=2, seconds=50)

    # The load test's incident: an error spike 24 minutes earlier that Medic could not fix.
    async with sf() as session:
        seed = await open_incident(
            session,
            workspace_id=workspace.id,
            resource_ids=[ids["api"]],
            detected_by="watcher",
            signals=[
                Signal(
                    rule="log_error_spike",
                    resource_id=ids["api"],
                    resource_name="checkout-api",
                    detail="8 error lines in the last 5 minutes",
                    at=t0 - timedelta(minutes=24),
                )
            ],
            now=t0 - timedelta(minutes=23),
        )
        found_incident = await session.get(Incident, seed.incident.id)
        found_run = await session.get(Run, seed.run.id)
        assert found_incident is not None and found_run is not None
        await set_status(session, found_incident, found_run, "failed", reason="No fix")
        await session.commit()

    ev = _evidence(
        {
            "rds:checkout-orders-db": [
                (-24, "CPUUtilization 41.0% (baseline 4.0%) [SPIKE]"),  # the load test
                (-1, "CPUUtilization 56.5% (baseline 4.0%) [SPIKE]"),
            ],
            f"github:{REPO}": [
                (-3, f"commit {SHA[:7]} by dev: Drop customer_email index | files: {DROPPED}"),
                (-2.84, f"workflow run deploy.yml #57 started at {SHA[:7]}"),
            ],
            "ecs:checkout-api": [(-0.5, "(service checkout-api) failed container health checks.")],
            "cloudwatch_logs:/ecs/checkout-api": [
                *((-1.4 + n * 0.1, _app_error(n)) for n in range(8)),
                (-1.0, '{"level": "error", "msg": "GET /health failed", "status": 503}'),
            ],
        },
        ids,
        [
            ChangeMarker(
                kind="commit",
                ref=SHA,
                at=t0 - timedelta(minutes=3),
                label=f"{SHA[:7]} Drop orders.customer_email index",
                commit=SHA,
                repo=REPO,
            ),
            ChangeMarker(
                kind="deploy",
                ref=RUN_ID,
                at=change_at,
                label=f"deploy.yml in_progress at {SHA[:7]}",
                commit=SHA,
                repo=REPO,
            ),
        ],
        t0,
    )
    early = eid(ev, "rds:checkout-orders-db", "41.0%")
    late = eid(ev, "rds:checkout-orders-db", "56.5%")
    errors = eid(ev, "cloudwatch_logs:/ecs/checkout-api", "order search failed")
    rc = json.dumps(
        {
            "summary": "A migration dropped orders.customer_email; searches scan the table.",
            "root_cause": f"Commit {SHA[:7]} dropped the index when deploy {RUN_ID} started.",
            "claims": [
                {"text": "RDS spiked from 10:16.", "evidence_ids": [early]},
                {"text": "RDS CPU 56.5% vs 4%.", "evidence_ids": [early, late]},
                {"text": "asyncpg timeouts on /orders/search.", "evidence_ids": [errors]},
            ],
            "triggering_change": {
                "kind": "deploy",
                "ref": RUN_ID,
                "at": change_at.isoformat(),
                "description": f"deploy.yml at commit {SHA[:7]}; its migration dropped the index",
                "evidence_ids": [eid(ev, f"github:{REPO}", "started")],
            },
            "affected_resources": ["checkout-api", "checkout-orders-db"],
            "confidence": "high",
            "fix_options": [{"kind": "pull_request", "description": "recreate the index"}],
            "search_query": ROOT_QUERY,
        }
    )
    fix = json.dumps(
        {
            "title": "Recreate orders.customer_email index concurrently",
            "body": "Adds a migration that recreates the index dropped by b11275e.",
            "rationale": "Searches on customer_email scan the whole table without it.",
            "files": [
                {
                    "path": MIGRATION,
                    "content": (
                        "revision = '0005'\ndown_revision = '0004'\n\ndef upgrade():\n"
                        "    with op.get_context().autocommit_block():\n"
                        "        op.execute('CREATE INDEX CONCURRENTLY ix_orders_customer_email "
                        "ON orders (customer_email)')\n"
                    ),
                }
            ],
        }
    )
    searched = Recorded()
    research = TavilyClient(
        settings(),
        transport=mock_transport(
            {
                "POST /search": {
                    "results": [
                        {"title": "Index scans", "url": "https://pg.example/idx", "content": "..."}
                    ]
                }
            },
            searched,
        ),
    )
    llm = FakeLLM(reply(rc), reply(fix), settings=settings(dev_mode_no_ultra=True))
    h = Harness(
        sf,
        ev,
        llm,
        actions=FakeEmergencyActions(sf),
        gh=github_routes(SHA, {DROPPED: ("+op.drop_index('ix_orders_customer_email')", "x\n")}),
        research=research,
    )
    use_deps(h)
    opened_at = t0 - timedelta(minutes=signal_minutes)
    async with sf() as session:
        opened = await open_incident(
            session,
            workspace_id=workspace.id,
            resource_ids=[ids["api"]],
            detected_by="watcher",
            signals=[
                Signal(
                    rule="log_error_spike",
                    resource_id=ids["api"],
                    resource_name="checkout-api",
                    detail="59 error lines in the last 5 minutes",
                    at=opened_at,
                )
            ],
            now=t0,
        )
    assert not opened.deduped
    await investigate_incident_job(
        sf, {"incident_id": str(opened.incident.id), "run_id": str(opened.run.id)}
    )
    incident, state, _ = await _state(sf, opened.incident.id)

    # 1. The fix PR, in the repo the deploy named, through the installation.
    assert [f.fix.kind for f in state.fixes] == ["pr"], state.notes
    assert not [n for n in state.notes if n.startswith("No fix PR")]
    view = state.fixes[0].fix.pull_request
    assert view is not None and view.repo == REPO
    assert view.files[0].path == MIGRATION and "CONCURRENTLY" in view.files[0].diff
    assert state.fixes[0].draft is not None
    assert state.fixes[0].draft.installation_id == INSTALLATION

    # 2. Timed from the opening signal (or the deploy, when the signal came first), never the
    # load test's; the load test's spike is not part of this root cause.
    assert incident.first_signal_at == max(opened_at, change_at)
    assert state.root_cause is not None
    cited = {i for c in state.root_cause.claims for i in c.evidence_ids}
    assert early not in cited and late in cited
    assert [c.text for c in state.root_cause.claims] == [
        "RDS CPU 56.5% vs 4%.",
        "asyncpg timeouts on /orders/search.",
    ]
    assert any("spikes before the triggering change" in d for d in state.dropped)

    # 3. The dominant app error first, then the root cause, within the cap of 2.
    sent = [json.loads(r.content)["query"] for r in searched.requests]
    assert len(sent) == 2 and sent == state.research_queries
    assert "asyncpg" in sent[0] and "statement timeout" in sent[0] and "health" not in sent[0]
    # The root-cause query carries the app's stack (from its lines and the RDS engine).
    assert sent[1] == f"{ROOT_QUERY} asyncpg sqlalchemy"
    assert state.tavily_calls == 2
    fix_prompt = llm.calls[-1]["messages"][-1]["content"]
    assert "Background from web research" in fix_prompt and "Index scans" in fix_prompt


# --- units ------------------------------------------------------------------------------------


def _root(ref: str, kind: str = "deploy", description: str = "") -> RootCause:
    return RootCause(
        summary="s",
        root_cause="r",
        confidence="high",
        triggering_change=TriggeringChange(kind=kind, ref=ref, description=description),
    )


def test_a_deploy_run_id_resolves_to_the_commit_it_shipped() -> None:
    t = _now()
    deploy = ChangeMarker(kind="deploy", ref=RUN_ID, at=t, label="d", commit=SHA, repo=REPO)
    # Only the deploy in the window (the commit was pushed earlier than the lookback).
    assert fx.commit_change(_root(RUN_ID), [deploy], None) == (SHA, deploy)
    # The old reading: eleven digits looked like a SHA prefix.
    assert fx._SHA.search(RUN_ID) is None
    # A short SHA in the description still finds the deploy that ran it.
    assert fx.commit_change(_root("deploy.yml", description=f"at {SHA[:7]}"), [deploy], None) == (
        SHA,
        deploy,
    )


async def test_repo_missing_from_the_graph_is_found_in_the_installations_repos() -> None:
    conn = Connection(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        kind="github",
        name="gh",
        status="connected",
        config={"installation_id": INSTALLATION, "repos": []},
    )
    graph = fx.Graph(resources={}, edges=[], connections={conn.id: conn}, affected=[])
    marker = ChangeMarker(kind="commit", ref=SHA, at=_now(), label="c", repo=REPO.lower())
    recorded = Recorded()
    target = await fx.resolve_repo(graph, marker, github_app(recorded))
    assert isinstance(target, fx.RepoTarget)
    assert (target.installation_id, target.name) == (INSTALLATION, REPO)
    assert "GET /installation/repositories" in recorded.paths()

    other = marker.model_copy(update={"repo": "someone/else"})
    assert await fx.resolve_repo(graph, other, github_app()) == (
        "No fix PR: someone/else is not in any GitHub connection of this workspace."
    )


async def test_a_repo_resource_wins_without_calling_github(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    ids = await demo_graph(session_factory, workspace.id)
    async with session_factory() as session:
        repo = await session.get(Resource, ids["repo"])
        assert repo is not None and repo.connection_id is not None
        conn = await session.get(Connection, repo.connection_id)
        assert conn is not None
    graph = fx.Graph({repo.id: repo}, [], {conn.id: conn}, [])
    recorded = Recorded()
    target = await fx.resolve_repo(graph, None, github_app(recorded))
    assert target == fx.RepoTarget(INSTALLATION, REPO, "main")
    assert recorded.requests == []


def test_the_dominant_application_error_is_the_research_query() -> None:
    lines = [_app_error(n) for n in range(3)] + [
        '{"level": "error", "msg": "GET /health failed"}',
        "latency summary path=/orders/search requests=12 p50=13860ms errors=9",
        "reader failed: timeout",
    ]
    query = dominant_error(lines)
    assert query.startswith("order search failed sqlalchemy.exc.DBAPIError")
    assert "deadbeef" not in query
    assert dominant_error(["GET /orders 200 in 12ms"]) == ""
