"""HTTP API: runs, approvals, SSE, workspaces, stop, request ids. LLM is faked.

Requests carry an internal JWT minted exactly like the web app's proxy does.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, text

from app.accounts import upsert_user
from app.agents.base import AgentRunner
from app.config import Settings, get_settings
from app.db.models import (
    Approval,
    ApprovalStatus,
    Job,
    Run,
    RunStatus,
    RunStep,
    ToolPolicy,
    Workspace,
)
from app.db.session import SessionFactory, get_session_factory
from app.llm.budget import PgUsageStore
from app.main import app
from app.security.auth import WebIdentity
from app.worker import Worker
from tests.auth_helpers import TEST_JWT_SECRET, auth_headers
from tests.fakes import FakeLLM, reply, tool_call

pytestmark = pytest.mark.anyio


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "model_nano": "test/nano",
        "sse_poll_interval_ms": 20,
        "internal_jwt_secret": SecretStr(TEST_JWT_SECRET),
        **overrides,
    }
    return Settings(_env_file=None, **values)


@pytest.fixture
async def client(session_factory: SessionFactory) -> AsyncIterator[httpx.AsyncClient]:
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = lambda: _settings()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@dataclass
class Member:
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    headers: dict[str, str]


async def _member(sf: SessionFactory, github_id: int, login: str) -> Member:
    async with sf() as session:
        user = await upsert_user(session, WebIdentity(github_id=github_id, login=login))
        workspace = await session.scalar(select(Workspace).where(Workspace.owner_id == user.id))
        assert workspace is not None
        return Member(user.id, workspace.id, auth_headers(github_id, login))


@pytest.fixture
async def alice(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 1001, "alice")


@pytest.fixture
async def bob(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 2002, "bob")


def _parse_sse(body: str) -> list[tuple[str, dict[str, Any], str | None]]:
    events = []
    for block in body.strip().split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in block.splitlines() if not line.startswith(":")
        )
        if "event" in fields:
            events.append((fields["event"], json.loads(fields["data"]), fields.get("id")))
    return events


async def _stream_url(client: httpx.AsyncClient, member: Member, run_id: object) -> str:
    resp = await client.post(f"/runs/{run_id}/stream-token", headers=member.headers)
    assert resp.status_code == 200, resp.text
    return f"/runs/{run_id}/events?token={resp.json()['token']}"


async def _new_run(
    sf: SessionFactory, workspace_id: uuid.UUID, status: RunStatus = RunStatus.running
) -> uuid.UUID:
    async with sf() as session:
        run = Run(workspace_id=workspace_id, agent="echo_ops", goal="g", status=status)
        session.add(run)
        await session.commit()
        return run.id


async def _add_step(sf: SessionFactory, run_id: uuid.UUID, seq: int, text_: str) -> None:
    async with sf() as session:
        session.add(
            RunStep(run_id=run_id, seq=seq, agent="echo_ops", kind="tool", output_summary=text_)
        )
        await session.commit()


async def _set_status(sf: SessionFactory, run_id: uuid.UUID, status: RunStatus) -> None:
    async with sf() as session:
        run = await session.get_one(Run, run_id)
        run.status = status
        await session.commit()


def _toy_worker(sf: SessionFactory, *responses: Any) -> Worker:
    runner = AgentRunner(sf, llm=FakeLLM(*responses), settings=_settings())
    return Worker(sf, {"run_agent": runner.handle_job, "resume_run": runner.handle_job})


async def _drain(worker: Worker) -> None:
    while await worker.run_once():
        pass


async def test_create_run_enqueues_job(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    resp = await client.post(
        "/runs",
        json={
            "agent": "echo_ops",
            "goal": "List services",
            "workspace_id": str(alice.workspace_id),
        },
        headers=alice.headers,
    )
    assert resp.status_code == 202
    run_id = resp.json()["id"]
    assert resp.json()["status"] == "queued"
    async with session_factory() as session:
        job = await session.scalar(select(Job))
    assert job is not None and job.kind == "run_agent" and job.payload == {"run_id": run_id}


async def test_create_run_validation(client: httpx.AsyncClient, alice: Member) -> None:
    h = alice.headers
    bad_agent = await client.post(
        "/runs",
        json={"agent": "nope", "goal": "g", "workspace_id": str(alice.workspace_id)},
        headers=h,
    )
    assert bad_agent.status_code == 422
    no_ws = await client.post(
        "/runs",
        json={"agent": "echo_ops", "goal": "g", "workspace_id": str(uuid.uuid4())},
        headers=h,
    )
    assert no_ws.status_code == 404
    assert (await client.get(f"/runs/{uuid.uuid4()}", headers=h)).status_code == 404
    assert (await client.post(f"/runs/{uuid.uuid4()}/stream-token", headers=h)).status_code == 404


async def test_full_flow_pause_approve_complete(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    worker = _toy_worker(
        session_factory,
        reply(None, tool_call("list_fake_services")),
        reply(None, tool_call("restart_fake_service", name="api")),
        reply("Restarted api."),
    )
    h = alice.headers
    run_id = (
        await client.post(
            "/runs",
            json={
                "agent": "echo_ops",
                "goal": "List the services and restart the one named api.",
                "workspace_id": str(alice.workspace_id),
            },
            headers=h,
        )
    ).json()["id"]
    await _drain(worker)

    run = (await client.get(f"/runs/{run_id}", headers=h)).json()
    assert run["status"] == "waiting_approval" and run["stop_reason"] == "waiting_approval"
    assert "messages" not in run
    [inline] = run["approvals"]
    assert inline["status"] == "pending" and inline["run_goal"].startswith("List the services")
    tool_steps = [s for s in run["steps"] if s["tool_name"]]
    assert [s["tool_policy"] for s in tool_steps] == ["read", "write"]
    assert run["steps"][-1]["approval_id"] == inline["id"]

    pending = (
        await client.get(
            f"/workspaces/{alice.workspace_id}/approvals", params={"status": "pending"}, headers=h
        )
    ).json()
    assert [(a["tool_name"], a["arguments"], a["run_agent"]) for a in pending] == [
        ("restart_fake_service", {"name": "api"}, "echo_ops")
    ]

    decided = await client.post(
        f"/approvals/{pending[0]['id']}/decide", json={"decision": "approve"}, headers=h
    )
    assert decided.status_code == 200
    body = decided.json()
    assert body["status"] == "approved"
    assert body["decided_by"] == str(alice.user_id)
    assert body["decided_by_login"] == "alice"
    assert body["decided_by_avatar_url"] == "https://avatars.example.com/1001"
    again = await client.post(
        f"/approvals/{pending[0]['id']}/decide", json={"decision": "reject"}, headers=h
    )
    assert again.status_code == 409
    missing = await client.post(
        f"/approvals/{uuid.uuid4()}/decide", json={"decision": "reject"}, headers=h
    )
    assert missing.status_code == 404

    await _drain(worker)
    run = (await client.get(f"/runs/{run_id}", headers=h)).json()
    assert (run["status"], run["stop_reason"], run["result"]) == (
        "succeeded",
        "completed",
        "Restarted api.",
    )
    assert run["duration_ms"] is not None and run["total_tokens"] == 360
    assert run["rejected_actions"] == 0
    assert [s["kind"] for s in run["steps"]] == [
        "llm",
        "tool",
        "llm",
        "approval_requested",
        "approval_decided",
        "tool",
        "llm",
    ]
    assert run["steps"][4]["output_summary"] == "approved by alice"
    assert (
        await client.get(
            f"/workspaces/{alice.workspace_id}/approvals", params={"status": "pending"}, headers=h
        )
    ).json() == []
    history = (
        await client.get(
            f"/workspaces/{alice.workspace_id}/approvals", params={"decided": True}, headers=h
        )
    ).json()
    assert [a["decided_by_login"] for a in history] == ["alice"]

    events = _parse_sse((await client.get(await _stream_url(client, alice, run_id))).text)
    assert [e for e, _, _ in events] == [
        "step",
        "step",
        "step",
        "approval_requested",
        "approval_decided",
        "step",
        "step",
        "status",
        "done",
    ]
    assert [i for _, _, i in events[:7]] == [str(n) for n in range(1, 8)]
    requested = events[3][1]
    assert requested["approval"]["id"] == inline["id"]
    assert requested["step"]["kind"] == "approval_requested"
    assert events[4][1]["approval"]["decided_by_login"] == "alice"
    assert events[-1][1]["status"] == "succeeded"


async def test_sse_streams_new_steps_live(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    run_id = await _new_run(session_factory, alice.workspace_id)
    await _add_step(session_factory, run_id, 1, "existing")

    stream = asyncio.create_task(client.get(await _stream_url(client, alice, run_id)))
    await asyncio.sleep(0.2)  # the stream has started and sent step 1
    await _add_step(session_factory, run_id, 2, "live one")
    await _set_status(session_factory, run_id, RunStatus.waiting_approval)
    await asyncio.sleep(0.1)
    await _add_step(session_factory, run_id, 3, "live two")
    await _set_status(session_factory, run_id, RunStatus.succeeded)
    resp = await asyncio.wait_for(stream, timeout=5)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    steps = [d["output_summary"] for e, d, _ in events if e == "step"]
    statuses = [d["status"] for e, d, _ in events if e == "status"]
    assert steps == ["existing", "live one", "live two"]
    assert statuses == ["running", "waiting_approval", "succeeded"]
    assert events[-1][0] == "done"


async def test_sse_resumes_after_last_event_id(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    run_id = await _new_run(session_factory, alice.workspace_id, RunStatus.failed)
    for seq in (1, 2, 3):
        await _add_step(session_factory, run_id, seq, f"s{seq}")
    url = await _stream_url(client, alice, run_id)
    by_header = await client.get(url, headers={"Last-Event-ID": "2"})
    assert [d["seq"] for e, d, _ in _parse_sse(by_header.text) if e == "step"] == [3]
    # A fresh EventSource cannot set the header, so ?after= works too.
    by_query = await client.get(f"{await _stream_url(client, alice, run_id)}&after=1")
    assert [d["seq"] for e, d, _ in _parse_sse(by_query.text) if e == "step"] == [2, 3]


async def test_sse_does_not_hold_a_connection_between_polls(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    # A slow poll so the stream is idle between polls while we look.
    app.dependency_overrides[get_settings] = lambda: _settings(sse_poll_interval_ms=1000)
    run_id = await _new_run(session_factory, alice.workspace_id)
    urls = [await _stream_url(client, alice, run_id) for _ in range(3)]
    streams = [asyncio.create_task(client.get(u)) for u in urls]
    await asyncio.sleep(0.4)
    async with session_factory() as session:
        idle_in_tx = await session.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND state = 'idle in transaction' AND pid <> pg_backend_pid()"
            )
        )
    await _set_status(session_factory, run_id, RunStatus.succeeded)
    await asyncio.wait_for(asyncio.gather(*streams), timeout=5)
    assert idle_in_tx == 0


async def test_approval_arguments_are_redacted_for_display(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    run_id = await _new_run(session_factory, alice.workspace_id)
    async with session_factory() as session:
        approval = Approval(
            run_id=run_id,
            workspace_id=alice.workspace_id,
            tool_call_id="c1",
            tool_name="restart_fake_service",
            policy=ToolPolicy.write,
            arguments={"name": "api", "api_token": "abc123abc123", "url": "https://u:pw12345@h"},
            status=ApprovalStatus.pending,
        )
        session.add(approval)
        await session.commit()
    [shown] = (
        await client.get(f"/workspaces/{alice.workspace_id}/approvals", headers=alice.headers)
    ).json()
    assert shown["arguments"]["name"] == "api"
    assert "abc123abc123" not in json.dumps(shown) and "pw12345" not in json.dumps(shown)
    async with session_factory() as session:  # what runs is unchanged
        stored = await session.get_one(Approval, approval.id)
    assert stored.arguments["api_token"] == "abc123abc123"  # noqa: S105


async def test_runs_list_paginates_newest_first(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    ids = [await _new_run(session_factory, alice.workspace_id) for _ in range(5)]
    url = f"/workspaces/{alice.workspace_id}/runs"
    first = (await client.get(url, params={"limit": 2}, headers=alice.headers)).json()
    second = (
        await client.get(
            url, params={"limit": 2, "cursor": first["next_cursor"]}, headers=alice.headers
        )
    ).json()
    third = (
        await client.get(
            url, params={"limit": 2, "cursor": second["next_cursor"]}, headers=alice.headers
        )
    ).json()
    seen = [r["id"] for page in (first, second, third) for r in page["items"]]
    assert seen == [str(i) for i in reversed(ids)]
    assert third["next_cursor"] is None
    bad = await client.get(url, params={"cursor": "%%%"}, headers=alice.headers)
    assert bad.status_code == 422


async def test_me_and_summary(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    me = (await client.get("/me", headers=alice.headers)).json()
    assert me["user"]["github_login"] == "alice"
    assert me["workspaces"] == [
        {"id": str(alice.workspace_id), "name": "alice's workspace", "role": "owner"}
    ]
    run_id = await _new_run(session_factory, alice.workspace_id)
    async with session_factory() as session:
        session.add(
            RunStep(
                run_id=run_id,
                seq=1,
                agent="echo_ops",
                kind="llm",
                prompt_tokens=90,
                completion_tokens=10,
            )
        )
        await session.commit()
    # Spend across all workspaces comes from llm_usage (the client adds every call to it).
    await PgUsageStore(session_factory).add(Decimal("0.012"), 100)
    app.dependency_overrides[get_settings] = lambda: _settings(daily_token_budget=1000)
    summary = (
        await client.get(f"/workspaces/{alice.workspace_id}/summary", headers=alice.headers)
    ).json()
    assert summary == {
        "runs_today": 1,
        "pending_approvals": 0,
        "workspace_tokens_today": 100,
        "budget_tokens_today": 100,
        "daily_token_budget": 1000,
        "usd_today": 0.012,
        "daily_usd_cap": 2.0,
        "usd_total": 0.012,
        "total_usd_cap": 25.0,
        "pending_permanent_fixes": 0,
    }


async def test_other_users_cannot_see_a_workspace(
    client: httpx.AsyncClient, alice: Member, bob: Member, session_factory: SessionFactory
) -> None:
    run_id = await _new_run(session_factory, alice.workspace_id, RunStatus.waiting_approval)
    async with session_factory() as session:
        approval = Approval(
            run_id=run_id,
            workspace_id=alice.workspace_id,
            tool_call_id="c1",
            tool_name="restart_fake_service",
            policy=ToolPolicy.write,
            arguments={"name": "api"},
        )
        session.add(approval)
        await session.commit()

    b = bob.headers
    ws = alice.workspace_id
    for method, path, body in [
        ("GET", f"/workspaces/{ws}/runs", None),
        ("GET", f"/workspaces/{ws}/approvals", None),
        ("GET", f"/workspaces/{ws}/summary", None),
        ("GET", f"/runs/{run_id}", None),
        ("POST", f"/runs/{run_id}/stream-token", None),
        ("POST", f"/runs/{run_id}/stop", None),
        ("POST", f"/approvals/{approval.id}/decide", {"decision": "approve"}),
        ("POST", "/runs", {"agent": "echo_ops", "goal": "g", "workspace_id": str(ws)}),
    ]:
        resp = await client.request(method, path, json=body, headers=b)
        assert resp.status_code == 404, (method, path, resp.status_code)
    me = (await client.get("/me", headers=b)).json()
    assert [w["name"] for w in me["workspaces"]] == ["bob's workspace"]
    async with session_factory() as session:
        stored = await session.get_one(Approval, approval.id)
    assert stored.status == ApprovalStatus.pending


async def test_stop_running_and_waiting_runs(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    worker = _toy_worker(
        session_factory,
        reply(None, tool_call("restart_fake_service", name="api")),
        reply("never reached"),
    )
    run_id = (
        await client.post(
            "/runs",
            json={
                "agent": "echo_ops",
                "goal": "restart api",
                "workspace_id": str(alice.workspace_id),
            },
            headers=alice.headers,
        )
    ).json()["id"]
    await _drain(worker)
    assert (await client.get(f"/runs/{run_id}", headers=alice.headers)).json()[
        "status"
    ] == "waiting_approval"

    stopped = await client.post(f"/runs/{run_id}/stop", headers=alice.headers)
    assert stopped.status_code == 200
    assert (stopped.json()["status"], stopped.json()["stop_reason"]) == (
        "stopped",
        "stopped_by_user",
    )
    run = (await client.get(f"/runs/{run_id}", headers=alice.headers)).json()
    assert run["steps"][-1]["kind"] == "stopped"
    assert run["steps"][-1]["output_summary"] == "Stopped by alice"
    [approval] = run["approvals"]
    assert approval["status"] == "rejected" and approval["decided_by_login"] == "alice"
    assert run["rejected_actions"] == 1 and stopped.json()["rejected_actions"] == 1
    listed = (
        await client.get(f"/workspaces/{alice.workspace_id}/runs", headers=alice.headers)
    ).json()["items"]
    assert [r["rejected_actions"] for r in listed] == [1]
    assert (await client.post(f"/runs/{run_id}/stop", headers=alice.headers)).status_code == 409
    decide = await client.post(
        f"/approvals/{approval['id']}/decide", json={"decision": "approve"}, headers=alice.headers
    )
    assert decide.status_code == 409


async def test_stop_while_agent_is_working_is_kept(
    alice: Member, session_factory: SessionFactory
) -> None:
    """The user stops the run while the model is thinking: the agent must not overwrite it."""

    class StopDuringCall(FakeLLM):
        async def chat(self, task: Any, messages: Any, **kwargs: Any) -> Any:
            await _set_status(session_factory, run_id, RunStatus.stopped)
            return await super().chat(task, messages, **kwargs)

    run_id = await _new_run(session_factory, alice.workspace_id, RunStatus.queued)
    llm = StopDuringCall(reply(None, tool_call("list_fake_services")), reply("done"))
    await AgentRunner(session_factory, llm=llm, settings=_settings()).run(run_id)
    async with session_factory() as session:
        run = await session.get_one(Run, run_id)
        kinds = list(await session.scalars(select(RunStep.kind).where(RunStep.run_id == run_id)))
    assert run.status == RunStatus.stopped
    assert len(llm.requests) == 1 and "tool" not in kinds  # no tool ran after the stop


async def test_request_id_header(client: httpx.AsyncClient) -> None:
    generated = await client.get("/health")
    assert len(generated.headers["x-request-id"]) == 32
    kept = await client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert kept.headers["x-request-id"] == "abc-123"
    replaced = await client.get("/health", headers={"X-Request-ID": "bad id\nwith newline"})
    assert replaced.headers["x-request-id"] != "bad id\nwith newline"


async def test_cors_allows_only_the_web_origin(client: httpx.AsyncClient) -> None:
    ok = await client.options(
        "/runs/x/events",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
    )
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:3000"
    other = await client.options(
        "/runs/x/events",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in other.headers
