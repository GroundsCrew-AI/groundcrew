"""Deploy API contract (Milestone 1): request validation, secrets never echoed, the typed
deploy events on the run stream, OpenAPI coverage, the 501 stubs behind auth, and the
migration that adds no_progress, runs.state, and run_steps.data."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, get_args

import httpx
import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError

from app.config import get_settings
from app.db.models import Run, RunStatus, RunStep, StopReason
from app.db.session import SessionFactory, get_session_factory
from app.main import app
from app.pipeline.events import (
    DEPLOY_EVENT_NAMES,
    BuildAttempt,
    DeployPlan,
    DeployStage,
    EnvVarView,
    PlannedResource,
    RunFinished,
    StageStarted,
)
from app.pipeline.state import DeployRecord, PipelineState
from app.routes.deploy_schemas import DeployCreate, FixtureSource, GitHubRepoSource
from app.routes.schemas import SseEventName, StepOut
from app.steps import record_step
from tests.conftest import API_DIR
from tests.test_api import Member, _member, _parse_sse, _settings, _stream_url

pytestmark = pytest.mark.anyio

CONNECTION = uuid.uuid4()
TARGET = uuid.uuid4()
WORKSPACE = uuid.uuid4()


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "workspace_id": str(WORKSPACE),
        "repo": {
            "connection_id": str(CONNECTION),
            "repo": "GroundsCrew-AI/legacy-flask-app",
            "ref": "main",
        },
        "target_connection_id": str(TARGET),
        "env": [{"name": "DATABASE_URL", "value": "postgresql://a:hunter2@db/x", "secret": True}],
        "options": {"allow_missing_env": True},
    }
    return {**body, **overrides}


@pytest.fixture
async def client(session_factory: SessionFactory) -> AsyncIterator[httpx.AsyncClient]:
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = lambda: _settings()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def alice(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 2001, "alice")


# --- request schema -----------------------------------------------------------------------


def test_source_is_github_or_fixture() -> None:
    assert isinstance(DeployCreate.model_validate(_body()).repo, GitHubRepoSource)
    fixture = DeployCreate.model_validate(_body(repo={"fixture": "py-build-trap"}))
    assert isinstance(fixture.repo, FixtureSource)
    defaults = DeployCreate.model_validate(_body(env=[], options={}))
    assert defaults.env == [] and defaults.options.allow_missing_env is False


@pytest.mark.parametrize(
    "repo",
    [
        # Both shapes at once is ambiguous.
        {"fixture": "x", "connection_id": str(CONNECTION), "repo": "a/b", "ref": "main"},
        {"connection_id": str(CONNECTION), "repo": "not-owner-slash-name", "ref": "main"},
        {"connection_id": str(CONNECTION), "repo": "a/b", "ref": "main branch"},
        {"connection_id": str(CONNECTION), "repo": "a/b"},
        {"fixture": "../etc"},
        {},
    ],
)
def test_bad_sources_are_rejected(repo: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DeployCreate.model_validate(_body(repo=repo))


@pytest.mark.parametrize(
    "env",
    [
        [{"name": "A", "value": "1"}, {"name": "A", "value": "2"}],
        [{"name": "1BAD", "value": "x"}],
        [{"name": "A", "value": "x", "extra": 1}],
    ],
)
def test_bad_env_is_rejected(env: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        DeployCreate.model_validate(_body(env=env))


def test_unknown_top_level_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        DeployCreate.model_validate(_body(region="eu-west-1"))


def test_env_values_never_serialize() -> None:
    body = DeployCreate.model_validate(_body())
    assert "hunter2" not in body.model_dump_json()
    assert "hunter2" not in repr(body)
    assert body.env[0].value.get_secret_value().endswith("hunter2@db/x")


# --- events -------------------------------------------------------------------------------


def test_every_deploy_event_is_an_sse_event_name() -> None:
    assert set(DEPLOY_EVENT_NAMES) <= set(get_args(SseEventName))
    assert "no_progress" in {r.value for r in StopReason}


async def test_event_payload_round_trips_and_is_redacted(
    session_factory: SessionFactory, alice: Member
) -> None:
    attempt = BuildAttempt(
        attempt=1,
        dockerfile_source="generated",
        dockerfile_diff="FROM python:3.12-slim\n",
        ok=False,
        duration_ms=4200,
        failing_step="RUN pip install -r requirements.txt",
        error_summary="pg_config executable not found",
        log_tail="Error: pg_config executable not found\nAuthorization: Bearer ghp_" + "a" * 36,
    )
    async with session_factory() as session:
        run = Run(workspace_id=alice.workspace_id, agent="deploy", goal="g")
        session.add(run)
        await session.flush()
        step = await record_step(session, run, kind="build_attempt", agent="builder", data=attempt)
        await session.commit()

    out = StepOut.build(step)
    assert isinstance(out.data, BuildAttempt)
    assert out.data.error_summary == "pg_config executable not found"
    assert out.data.log_tail is not None and "ghp_" not in out.data.log_tail


async def test_unknown_payload_keeps_the_step(
    session_factory: SessionFactory, alice: Member
) -> None:
    async with session_factory() as session:
        run = Run(workspace_id=alice.workspace_id, agent="deploy", goal="g")
        session.add(run)
        await session.flush()
        step = RunStep(run_id=run.id, seq=1, agent="deploy", kind="tool", data={"type": "new"})
        session.add(step)
        await session.commit()
    assert StepOut.build(step).data is None


async def test_deploy_events_stream_under_their_own_names(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    payloads: list[StageStarted | BuildAttempt | RunFinished] = [
        StageStarted(stage="scout", agent="scout"),
        BuildAttempt(
            attempt=2,
            dockerfile_source="fixed",
            dockerfile_diff="+RUN apt-get install -y libpq-dev gcc\n",
            ok=True,
            duration_ms=31000,
            image_ref="gc-build/legacy-flask-app:abc",
        ),
        RunFinished(
            status=RunStatus.succeeded,
            stop_reason=StopReason.completed,
            live_url="http://legacy-flask-app.localhost",
            build_attempts=2,
            automatic_fix=True,
            duration_ms=60000,
            cost_usd=0.0021,
        ),
    ]
    async with session_factory() as session:
        run = Run(
            workspace_id=alice.workspace_id,
            agent="deploy",
            goal="g",
            status=RunStatus.succeeded,
            stop_reason=StopReason.completed,
        )
        session.add(run)
        await session.flush()
        await record_step(session, run, kind="tool", output_summary="plain step")
        for payload in payloads:
            await record_step(session, run, kind=payload.type, data=payload)
        await session.commit()
        run_id = run.id

    events = _parse_sse((await client.get(await _stream_url(client, alice, run_id))).text)
    names = [name for name, _, _ in events]
    assert names[:4] == ["step", "stage_started", "build_attempt", "run_finished"]
    assert [i for _, _, i in events[:4]] == ["1", "2", "3", "4"]
    build = events[2][1]
    assert build["data"]["type"] == "build_attempt" and build["data"]["attempt"] == 2
    assert events[3][1]["data"]["automatic_fix"] is True
    assert names[-1] == "done"

    # On reload, GET /runs/{id} returns the same typed payloads.
    detail = (await client.get(f"/runs/{run_id}", headers=alice.headers)).json()
    assert [s["data"]["type"] if s["data"] else None for s in detail["steps"]] == [
        None,
        "stage_started",
        "build_attempt",
        "run_finished",
    ]


def test_plan_shows_secrets_by_name_only() -> None:
    plan = DeployPlan(
        target="k8s",
        target_connection_id=TARGET,
        app="legacy-flask-app",
        image="kind-registry:5000/legacy-flask-app:abc",
        source_image="sha256:" + "3f2a" * 16,
        port=8000,
        health_path="/health",
        replicas=1,
        cpu="250m",
        memory="512Mi",
        env=[EnvVarView(name="DATABASE_URL", secret=True, required=True, provided=False)],
        resources=[
            PlannedResource(
                kind="image", name="kind-registry:5000/legacy-flask-app:abc", action="push"
            ),
            PlannedResource(kind="Deployment", name="legacy-flask-app", action="create"),
        ],
        preflight=[],
        allow_missing_env=True,
        rendered="kind: Secret\n",
        rendered_format="yaml",
    )
    assert plan.env[0].value is None


def test_pipeline_state_keeps_a_deploy_record() -> None:
    now = datetime.now(UTC)
    record = DeployRecord(
        target="aws_ecs",
        connection_id=TARGET,
        app="legacy-flask-app",
        image="881050425883.dkr.ecr.us-east-2.amazonaws.com/groundcrew-legacy-flask-app:abc",
        commit="abc",
        repo="GroundsCrew-AI/legacy-flask-app",
        plan_summary="ECS service, 1 task, 0.25 vCPU / 0.5 GB",
        live_url="http://203.0.113.10:8000",
        revision="arn:aws:ecs:us-east-2:881050425883:task-definition/groundcrew-legacy-flask-app:2",
        previous_revision=None,
        started_at=now,
        finished_at=now,
        status="live",
    )
    state = PipelineState.model_validate({"deploy": record, "future_key": 1})
    again = PipelineState.model_validate(state.model_dump(mode="json"))
    assert again.deploy == record
    assert again.model_extra == {"future_key": 1}


# --- OpenAPI ------------------------------------------------------------------------------


def test_openapi_has_the_deploy_contract() -> None:
    schema = app.openapi()
    assert {"/deploys", "/deploys/{deploy_id}", "/deploys/{deploy_id}/env"} <= set(schema["paths"])
    assert "409" in schema["paths"]["/deploys"]["post"]["responses"]
    components = schema["components"]["schemas"]
    for name in (
        "DeployCreate",
        "DeployCreated",
        "DeployEnvUpdate",
        "DeployEnvUpdated",
        "DeployPage",
        "DeploySummary",
        "DeployOut",  # the Map's recent deploys (discovery_schemas), unchanged
        "DeployDetail",
        "DeployRecord",
        "StageStarted",
        "StageFinished",
        "BuildAttempt",
        "DeployPlan",
        "VerifyResult",
        "LiveUrl",
        "RollbackOffered",
        "RunFinished",
    ):
        assert name in components, name
    # Two models with one name get module-qualified names and break the web types.
    assert not [name for name in components if "__" in name]
    assert "no_progress" in components["StopReason"]["enum"]
    assert "data" not in components["StepOut"].get("required", [])


# --- routes (the pipeline itself: tests/test_pipeline.py) ---------------------------------


async def test_routes_need_sign_in(client: httpx.AsyncClient) -> None:
    assert (await client.post("/deploys", json=_body())).status_code == 401
    assert (
        await client.get("/deploys", params={"workspace_id": str(WORKSPACE)})
    ).status_code == 401
    assert (await client.get(f"/deploys/{uuid.uuid4()}")).status_code == 401


async def test_non_members_get_404(client: httpx.AsyncClient, alice: Member) -> None:
    resp = await client.post("/deploys", json=_body(), headers=alice.headers)
    assert resp.status_code == 404
    resp = await client.get(
        "/deploys", params={"workspace_id": str(WORKSPACE)}, headers=alice.headers
    )
    assert resp.status_code == 404


async def test_members_get_real_answers(client: httpx.AsyncClient, alice: Member) -> None:
    params = {"workspace_id": str(alice.workspace_id)}
    resp = await client.get("/deploys", params=params, headers=alice.headers)
    assert resp.status_code == 200 and resp.json() == {"items": [], "next_cursor": None}
    resp = await client.get(f"/deploys/{uuid.uuid4()}", headers=alice.headers)
    assert resp.status_code == 404
    # A connection that is not in the workspace.
    body = _body(workspace_id=str(alice.workspace_id))
    assert (await client.post("/deploys", json=body, headers=alice.headers)).status_code == 404


async def test_bad_body_is_422_and_does_not_echo_secrets(
    client: httpx.AsyncClient, alice: Member
) -> None:
    body = _body(workspace_id=str(alice.workspace_id), region="x")
    resp = await client.post("/deploys", json=body, headers=alice.headers)
    assert resp.status_code == 422
    assert "hunter2" not in resp.text


@pytest.mark.parametrize(
    "env",
    [
        [{"name": "DB", "value": "hunter2" * 3000, "secret": True}],
        [{"name": "DB", "value": "hunter2", "secret": True}, {"name": "DB", "value": "hunter2"}],
    ],
)
async def test_env_errors_do_not_echo_values(
    client: httpx.AsyncClient, alice: Member, env: list[dict[str, Any]]
) -> None:
    body = _body(workspace_id=str(alice.workspace_id), env=env)
    resp = await client.post("/deploys", json=body, headers=alice.headers)
    assert resp.status_code == 422
    assert "hunter2" not in resp.text
    assert resp.json()["detail"][0]["loc"][:2] == ["body", "env"]


async def test_other_422s_keep_their_input(client: httpx.AsyncClient, alice: Member) -> None:
    resp = await client.post(
        "/runs", json={"agent": "echo_ops", "goal": "", "workspace_id": "x"}, headers=alice.headers
    )
    assert resp.status_code == 422
    assert all("input" in e for e in resp.json()["detail"])


# --- migration ----------------------------------------------------------------------------


def test_migration_downgrades_and_upgrades(database_url: str) -> None:
    config = Config(str(API_DIR / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.downgrade(config, "3c1d9e4a7b20")
    command.upgrade(config, "head")


async def test_no_progress_and_state_are_stored(
    session_factory: SessionFactory, alice: Member
) -> None:
    async with session_factory() as session:
        run = Run(
            workspace_id=alice.workspace_id,
            agent="deploy",
            goal="g",
            status=RunStatus.stopped,
            stop_reason=StopReason.no_progress,
            state={"stages": {"build": {"status": "failed"}}},
        )
        session.add(run)
        await session.commit()
        run_id = run.id
    async with session_factory() as session:
        stored = await session.get_one(Run, run_id)
        assert stored.stop_reason == StopReason.no_progress
        assert PipelineState.model_validate(stored.state).stages["build"].status == "failed"
    async with session_factory() as session:
        fresh = Run(workspace_id=alice.workspace_id, agent="echo_ops", goal="g")
        session.add(fresh)
        await session.commit()
        assert fresh.state == {}


def test_nothing_is_pushed_before_approval() -> None:
    # The image push is the first step of apply, not a stage before the approval.
    assert "push" not in get_args(DeployStage)
    stages = list(get_args(DeployStage))
    assert stages.index("smoke") < stages.index("approval") < stages.index("apply")
