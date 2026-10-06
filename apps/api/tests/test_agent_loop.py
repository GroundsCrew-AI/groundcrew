"""Agent loop: step recording, approval pause/resume, limits, and errors. LLM is faked."""

import uuid
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app import queue
from app.agents.base import REJECTED_MESSAGE, AgentRunner, pending_tool_calls
from app.config import Settings
from app.db.models import (
    Approval,
    ApprovalStatus,
    Run,
    RunStatus,
    RunStep,
    StopReason,
    Workspace,
)
from app.db.session import SessionFactory
from app.llm.client import LLMError
from app.policy.engine import Decision, decide_approval
from app.worker import Worker
from tests.fakes import FakeLLM, reply, tool_call

pytestmark = pytest.mark.anyio


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "model_nano": "test/nano",
        "model_super": "test/super",
        "model_ultra": "test/ultra",
        **overrides,
    }
    return Settings(_env_file=None, **values)


async def _create_run(
    sf: SessionFactory, ws: Workspace, goal: str = "List the services and restart api."
) -> uuid.UUID:
    async with sf() as session:
        run = Run(workspace_id=ws.id, agent="echo_ops", goal=goal)
        session.add(run)
        await session.flush()
        await queue.enqueue(session, "run_agent", {"run_id": str(run.id)})
        await session.commit()
        return run.id


async def _load(sf: SessionFactory, run_id: uuid.UUID) -> tuple[Run, list[RunStep]]:
    async with sf() as session:
        run = await session.get_one(Run, run_id)
        steps = (
            await session.scalars(
                select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.seq)
            )
        ).all()
        return run, list(steps)


def _worker(sf: SessionFactory, runner: AgentRunner) -> Worker:
    return Worker(sf, {"run_agent": runner.handle_job, "resume_run": runner.handle_job})


async def _drain(worker: Worker) -> None:
    while await worker.run_once():
        pass


async def _pause_at_restart(
    sf: SessionFactory, ws: Workspace, llm: FakeLLM
) -> tuple[uuid.UUID, AgentRunner, Approval]:
    runner = AgentRunner(sf, llm=llm, settings=_settings())
    run_id = await _create_run(sf, ws)
    await _drain(_worker(sf, runner))

    run, steps = await _load(sf, run_id)
    assert run.status == RunStatus.waiting_approval
    assert run.stop_reason == StopReason.waiting_approval
    assert [(s.seq, s.kind, s.tool_name) for s in steps] == [
        (1, "llm", None),
        (2, "tool", "list_fake_services"),
        (3, "llm", None),
        (4, "approval_requested", "restart_fake_service"),
    ]
    assert steps[1].output_summary == "api, worker, web"
    async with sf() as session:
        approval = await session.scalar(select(Approval).where(Approval.run_id == run_id))
    assert approval is not None and approval.status == ApprovalStatus.pending
    assert approval.arguments == {"name": "api"}
    return run_id, runner, approval


def _toy_script(final: str) -> FakeLLM:
    return FakeLLM(
        reply(None, tool_call("list_fake_services")),
        reply(None, tool_call("restart_fake_service", name="api")),
        reply(final),
    )


async def test_approve_resumes_and_completes(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    llm = _toy_script("Listed 3 services and restarted api.")
    run_id, runner, approval = await _pause_at_restart(session_factory, workspace, llm)

    async with session_factory() as session:
        await decide_approval(session, approval.id, Decision.approve, "tester")
    await _drain(_worker(session_factory, runner))

    run, steps = await _load(session_factory, run_id)
    assert (run.status, run.stop_reason) == (RunStatus.succeeded, StopReason.completed)
    assert run.result == "Listed 3 services and restarted api."
    assert run.finished_at is not None
    assert [s.kind for s in steps[4:]] == ["approval_decided", "tool", "llm"]
    assert steps[5].output_summary == "restarted api"
    assert llm.tool_results()[-1] == "restarted api"
    assert (run.prompt_tokens, run.completion_tokens) == (300, 60)
    assert all(s.model_tier == "nano" for s in steps if s.kind == "llm")


async def test_reject_feeds_rejection_back_and_completes(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    llm = _toy_script("Listed the services. The restart was rejected, so api was not restarted.")
    run_id, runner, approval = await _pause_at_restart(session_factory, workspace, llm)

    async with session_factory() as session:
        await decide_approval(session, approval.id, Decision.reject, "tester")
    await _drain(_worker(session_factory, runner))

    run, steps = await _load(session_factory, run_id)
    assert (run.status, run.stop_reason) == (RunStatus.succeeded, StopReason.completed)
    assert llm.tool_results()[-1] == REJECTED_MESSAGE
    assert "restarted api" not in [s.output_summary for s in steps]
    async with session_factory() as session:
        stored = await session.get_one(Approval, approval.id)
    assert stored.status == ApprovalStatus.rejected and stored.executed_at is None


async def test_resume_job_for_undecided_run_does_nothing(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    llm = _toy_script("done")
    run_id, runner, _ = await _pause_at_restart(session_factory, workspace, llm)
    await runner.run(run_id)  # e.g. a duplicate job
    run, steps = await _load(session_factory, run_id)
    assert run.status == RunStatus.waiting_approval and len(steps) == 4
    assert len(llm.requests) == 2


async def test_max_steps(session_factory: SessionFactory, workspace: Workspace) -> None:
    llm = FakeLLM(reply(None, tool_call("get_current_time")), repeat_last=True)
    runner = AgentRunner(session_factory, llm=llm, settings=_settings(max_steps_per_run=3))
    run_id = await _create_run(session_factory, workspace)
    await runner.run(run_id)
    run, steps = await _load(session_factory, run_id)
    assert (run.status, run.stop_reason) == (RunStatus.stopped, StopReason.max_steps)
    assert [s.kind for s in steps].count("llm") == 3


async def test_run_token_budget_stops_run(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    llm = FakeLLM(reply(None, tool_call("get_current_time")), repeat_last=True)
    runner = AgentRunner(session_factory, llm=llm, settings=_settings(max_tokens_per_run=200))
    run_id = await _create_run(session_factory, workspace)
    await runner.run(run_id)
    run, steps = await _load(session_factory, run_id)
    assert (run.status, run.stop_reason) == (RunStatus.stopped, StopReason.budget)
    assert len(llm.requests) == 2  # 120 tokens, then 240 >= 200
    assert steps[-1].kind == "budget" and "MAX_TOKENS_PER_RUN" in (steps[-1].output_summary or "")


async def test_daily_budget_stops_before_any_call(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    llm = FakeLLM(reply("first"), reply("second"))
    runner = AgentRunner(session_factory, llm=llm, settings=_settings())
    await runner.run(await _create_run(session_factory, workspace))  # uses 120 tokens today

    limited = AgentRunner(session_factory, llm=llm, settings=_settings(daily_token_budget=100))
    run_id = await _create_run(session_factory, workspace)
    await limited.run(run_id)
    run, _ = await _load(session_factory, run_id)
    assert run.stop_reason == StopReason.budget
    assert len(llm.requests) == 1


async def test_llm_error_fails_run(session_factory: SessionFactory, workspace: Workspace) -> None:
    llm = FakeLLM(LLMError("Token Factory call failed: 503"))
    runner = AgentRunner(session_factory, llm=llm, settings=_settings())
    run_id = await _create_run(session_factory, workspace)
    await runner.run(run_id)
    run, steps = await _load(session_factory, run_id)
    assert (run.status, run.stop_reason) == (RunStatus.failed, StopReason.error)
    assert run.error and "503" in run.error
    assert steps[-1].kind == "error"


async def test_unknown_agent_fails_run(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    async with session_factory() as session:
        run = Run(workspace_id=workspace.id, agent="nope", goal="g")
        session.add(run)
        await session.commit()
    await AgentRunner(session_factory, llm=FakeLLM(), settings=_settings()).run(run.id)
    stored, _ = await _load(session_factory, run.id)
    assert stored.stop_reason == StopReason.error


async def test_missing_token_factory_config_fails_cleanly(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    settings = _settings(token_factory_base_url=None, token_factory_api_key=None)
    run_id = await _create_run(session_factory, workspace)
    await AgentRunner(session_factory, settings=settings).run(run_id)
    run, _ = await _load(session_factory, run_id)
    assert run.stop_reason == StopReason.error
    assert run.error and "TOKEN_FACTORY_API_KEY" in run.error


async def test_bad_tool_calls_are_reported_to_the_model(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    llm = FakeLLM(
        reply(
            None,
            tool_call("delete_everything"),
            tool_call("restart_fake_service", wrong="api"),
        ),
        reply("Could not do that."),
    )
    runner = AgentRunner(session_factory, llm=llm, settings=_settings())
    run_id = await _create_run(session_factory, workspace)
    await runner.run(run_id)
    run, _ = await _load(session_factory, run_id)
    assert run.stop_reason == StopReason.completed
    results = llm.tool_results()
    assert "not available" in results[0]
    assert "invalid arguments" in results[1]
    async with session_factory() as session:
        assert await session.scalar(select(Approval)) is None


async def test_steps_are_redacted_and_truncated(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    secret = "sk-live-abcdefghijklmnop1234"  # noqa: S105
    llm = FakeLLM(reply(f"Found key {secret}. " + "x" * 5000))
    runner = AgentRunner(session_factory, llm=llm, settings=_settings())
    run_id = await _create_run(session_factory, workspace, goal=f"password=hunter2hunter2 {secret}")
    await runner.run(run_id)
    _, steps = await _load(session_factory, run_id)
    [step] = steps
    for text in (step.input_summary or "", step.output_summary or ""):
        assert secret not in text and "hunter2hunter2" not in text
        assert len(text) <= 2000
    assert (step.prompt_tokens, step.completion_tokens, step.latency_ms) == (100, 20, 5)
    assert step.model_id == "test/nano" and step.model_tier == "nano"


async def test_cost_recorded_when_prices_set(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    settings = _settings(price_nano_input_per_m=1.0, price_nano_output_per_m=2.0)
    runner = AgentRunner(session_factory, llm=FakeLLM(reply("ok")), settings=settings)
    run_id = await _create_run(session_factory, workspace)
    await runner.run(run_id)
    run, [step] = await _load(session_factory, run_id)
    assert step.cost_usd is not None and float(step.cost_usd) == pytest.approx(0.00014)
    assert run.cost_usd == step.cost_usd


def test_pending_tool_calls() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "a", "function": {"name": "x", "arguments": "{}"}},
                {"id": "b", "function": {"name": "y", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "done"},
    ]
    assert [c["id"] for c in pending_tool_calls(messages)] == ["b"]
    assert pending_tool_calls(messages[:1]) == []


def test_settings_secret_type() -> None:
    # Guard: the API key stays a SecretStr so it never shows in reprs or logs.
    s = _settings(token_factory_api_key=SecretStr("sk-abc"))
    assert "sk-abc" not in repr(s)
