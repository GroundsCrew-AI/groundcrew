"""HTTP API: runs, approvals, SSE, dev API key, request ids. LLM is faked."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.agents.base import AgentRunner
from app.config import Settings, get_settings
from app.db.models import Job, Run, RunStatus, RunStep, StopReason, Workspace
from app.db.session import SessionFactory, get_session_factory
from app.main import app
from app.worker import Worker
from tests.fakes import FakeLLM, reply, tool_call

pytestmark = pytest.mark.anyio


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"model_nano": "test/nano", "sse_poll_interval_ms": 20, **overrides}
    return Settings(_env_file=None, **values)


@pytest.fixture
async def client(session_factory: SessionFactory) -> AsyncIterator[httpx.AsyncClient]:
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = lambda: _settings()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in body.strip().split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in block.splitlines() if not line.startswith(":")
        )
        if "event" in fields:
            events.append((fields["event"], json.loads(fields["data"])))
    return events


async def test_create_run_enqueues_job(
    client: httpx.AsyncClient, workspace: Workspace, session_factory: SessionFactory
) -> None:
    resp = await client.post(
        "/runs",
        json={"agent": "echo_ops", "goal": "List services", "workspace_id": str(workspace.id)},
    )
    assert resp.status_code == 202
    run_id = resp.json()["id"]
    assert resp.json()["status"] == "queued"
    async with session_factory() as session:
        job = await session.scalar(select(Job))
    assert job is not None and job.kind == "run_agent" and job.payload == {"run_id": run_id}


async def test_create_run_validation(client: httpx.AsyncClient, workspace: Workspace) -> None:
    bad_agent = await client.post(
        "/runs", json={"agent": "nope", "goal": "g", "workspace_id": str(workspace.id)}
    )
    assert bad_agent.status_code == 422
    no_ws = await client.post(
        "/runs", json={"agent": "echo_ops", "goal": "g", "workspace_id": str(uuid.uuid4())}
    )
    assert no_ws.status_code == 404
    assert (await client.get(f"/runs/{uuid.uuid4()}")).status_code == 404
    assert (await client.get(f"/runs/{uuid.uuid4()}/events")).status_code == 404


async def test_full_flow_pause_approve_complete(
    client: httpx.AsyncClient, workspace: Workspace, session_factory: SessionFactory
) -> None:
    llm = FakeLLM(
        reply(None, tool_call("list_fake_services")),
        reply(None, tool_call("restart_fake_service", name="api")),
        reply("Restarted api."),
    )
    runner = AgentRunner(session_factory, llm=llm, settings=_settings())
    worker = Worker(
        session_factory, {"run_agent": runner.handle_job, "resume_run": runner.handle_job}
    )

    run_id = (
        await client.post(
            "/runs",
            json={
                "agent": "echo_ops",
                "goal": "List the services and restart the one named api.",
                "workspace_id": str(workspace.id),
            },
        )
    ).json()["id"]
    while await worker.run_once():
        pass

    run = (await client.get(f"/runs/{run_id}")).json()
    assert run["status"] == "waiting_approval" and run["stop_reason"] == "waiting_approval"
    assert "messages" not in run
    pending = (await client.get("/approvals", params={"status": "pending"})).json()
    assert [(a["tool_name"], a["arguments"]) for a in pending] == [
        ("restart_fake_service", {"name": "api"})
    ]

    decided = await client.post(
        f"/approvals/{pending[0]['id']}/decide", json={"decision": "approve"}
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "approved" and decided.json()["decided_by"]
    again = await client.post(f"/approvals/{pending[0]['id']}/decide", json={"decision": "reject"})
    assert again.status_code == 409
    missing = await client.post(f"/approvals/{uuid.uuid4()}/decide", json={"decision": "reject"})
    assert missing.status_code == 404

    while await worker.run_once():
        pass
    run = (await client.get(f"/runs/{run_id}")).json()
    assert (run["status"], run["stop_reason"], run["result"]) == (
        "succeeded",
        "completed",
        "Restarted api.",
    )
    assert [s["kind"] for s in run["steps"]] == [
        "llm",
        "tool",
        "llm",
        "approval_requested",
        "approval_decided",
        "tool",
        "llm",
    ]
    assert (await client.get("/approvals", params={"status": "pending"})).json() == []

    events = _parse_sse((await client.get(f"/runs/{run_id}/events")).text)
    assert [e for e, _ in events] == ["step"] * 7 + ["status", "end"]
    assert events[-1][1]["status"] == "succeeded"


async def _add_step(sf: SessionFactory, run_id: uuid.UUID, seq: int, text: str) -> None:
    async with sf() as session:
        session.add(
            RunStep(run_id=run_id, seq=seq, agent="echo_ops", kind="tool", output_summary=text)
        )
        await session.commit()


async def _set_status(sf: SessionFactory, run_id: uuid.UUID, status: RunStatus) -> None:
    async with sf() as session:
        run = await session.get_one(Run, run_id)
        run.status = status
        run.stop_reason = StopReason.completed if status == RunStatus.succeeded else None
        await session.commit()


async def test_sse_streams_new_steps_live(
    client: httpx.AsyncClient, workspace: Workspace, session_factory: SessionFactory
) -> None:
    async with session_factory() as session:
        run = Run(workspace_id=workspace.id, agent="echo_ops", goal="g", status=RunStatus.running)
        session.add(run)
        await session.commit()
    await _add_step(session_factory, run.id, 1, "existing")

    stream = asyncio.create_task(client.get(f"/runs/{run.id}/events"))
    await asyncio.sleep(0.2)  # the stream has started and sent step 1
    await _add_step(session_factory, run.id, 2, "live one")
    await _set_status(session_factory, run.id, RunStatus.waiting_approval)
    await asyncio.sleep(0.1)
    await _add_step(session_factory, run.id, 3, "live two")
    await _set_status(session_factory, run.id, RunStatus.succeeded)
    resp = await asyncio.wait_for(stream, timeout=5)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    steps = [d["output_summary"] for e, d in events if e == "step"]
    statuses = [d["status"] for e, d in events if e == "status"]
    assert steps == ["existing", "live one", "live two"]
    assert statuses == ["running", "waiting_approval", "succeeded"]
    assert events[-1][0] == "end"


async def test_sse_resumes_after_last_event_id(
    client: httpx.AsyncClient, workspace: Workspace, session_factory: SessionFactory
) -> None:
    async with session_factory() as session:
        run = Run(workspace_id=workspace.id, agent="echo_ops", goal="g", status=RunStatus.failed)
        session.add(run)
        await session.commit()
    for seq in (1, 2, 3):
        await _add_step(session_factory, run.id, seq, f"s{seq}")
    resp = await client.get(f"/runs/{run.id}/events", headers={"Last-Event-ID": "2"})
    assert [d["seq"] for e, d in _parse_sse(resp.text) if e == "step"] == [3]


async def test_dev_api_key(client: httpx.AsyncClient) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(
        dev_api_key=SecretStr("dev-key-123456")
    )
    assert (await client.get("/approvals")).status_code == 401
    assert (await client.get("/approvals", headers={"X-API-Key": "wrong"})).status_code == 401
    ok = await client.get("/approvals", headers={"X-API-Key": "dev-key-123456"})
    assert ok.status_code == 200
    assert (await client.get("/health")).status_code == 200


async def test_request_id_header(client: httpx.AsyncClient) -> None:
    generated = await client.get("/health")
    assert len(generated.headers["x-request-id"]) == 32
    kept = await client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert kept.headers["x-request-id"] == "abc-123"
    replaced = await client.get("/health", headers={"X-Request-ID": "bad id\nwith newline"})
    assert replaced.headers["x-request-id"] != "bad id\nwith newline"
