"""Our own tool-calling agent loop (no framework).

call model -> run tool calls through the policy engine -> append results -> repeat.

The conversation lives in runs.messages, so a run can pause for approval and resume in a
later job. Tool calls in the last assistant message that have no result yet are "pending";
each pass of the loop handles them first. On resume that runs the approved action, or
tells the model it was rejected.

Every exit sets runs.stop_reason: completed, max_steps, budget, waiting_approval, error.
A user can stop a run at any time (POST /runs/{id}/stop sets stopped_by_user); the loop checks
before every model and tool call and exits without overwriting that.
Runs execute in the worker ("run_agent" / "resume_run" jobs), never in an HTTP request.

No database transaction stays open while the model or a tool runs: the session commits
before each call, so a slow call never pins a pooled connection.
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import TERMINAL_RUN_STATUSES, Run, RunStatus, RunStep, StopReason
from app.db.session import SessionFactory
from app.llm.budget import BudgetExceededError, check_budget, estimate_prompt_tokens
from app.llm.client import ChatModel, LLMError, LLMResponse, Message, TokenFactoryClient
from app.llm.costs import estimate_cost
from app.llm.router import TaskType, route
from app.llm.tool_calls import parse_arguments
from app.policy.engine import (
    AwaitingApproval,
    Executed,
    PolicyViolationError,
    Rejected,
    RunStoppedError,
    handle_tool_call,
)
from app.security.redact import redact, summarize
from app.steps import record_step
from app.tools.registry import ToolNotFoundError, ToolRegistry, registry

logger = logging.getLogger("groundcrew.agents")

# Tool output sent back to the model is redacted and capped.
MAX_TOOL_RESULT_CHARS = 8000


def rejected_message(tool_name: str, arguments: dict[str, Any]) -> str:
    """What the model is told when a person rejects its action. Names the exact action, so
    the model reports the rejection instead of inventing another reason."""
    return (
        f"Rejected by the user: {tool_name}({json.dumps(arguments)}) was not run. "
        "Do not retry it. In your answer, say plainly that the user rejected this action; "
        "do not give any other reason for it not happening."
    )


@dataclass(frozen=True)
class ToolResult:
    """What a pending tool call produced. `rejected` means a person said no: it did not run."""

    text: str
    rejected: bool = False


@dataclass(frozen=True)
class Agent:
    name: str
    system_prompt: str
    tools: tuple[str, ...]
    task_type: TaskType


class UnknownAgentError(KeyError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _used_tool_call_ids(messages: list[Message]) -> set[str]:
    return {
        call["id"]
        for m in messages
        if m.get("role") == "assistant"
        for call in m.get("tool_calls") or []
    }


def _assistant_message(resp: LLMResponse, used_ids: set[str] | None = None) -> Message:
    """The assistant message to store.

    Tool call IDs are made unique within the run, because approvals are keyed by
    (run_id, tool_call_id) and a provider may reuse IDs across turns. Arguments the model
    sent as invalid JSON are kept in private `x_` keys (never sent to the provider), so the
    record shows what the model really sent and the tool is not run with `{}`.
    """
    message: Message = {"role": "assistant", "content": resp.content}
    if resp.tool_calls:
        seen = set(used_ids or ())
        calls: list[dict[str, Any]] = []
        for call in resp.tool_calls:
            call_id = call.id
            if not call_id or call_id in seen:
                call_id = f"call_gc_{uuid.uuid4().hex[:16]}"
            seen.add(call_id)
            entry: dict[str, Any] = {
                "id": call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            if call.arguments_error:
                entry["x_arguments_error"] = call.arguments_error
                entry["x_raw_arguments"] = call.raw_arguments
            calls.append(entry)
        message["tool_calls"] = calls
    return message


def _describe(message: Message) -> str:
    """Short description of the newest message sent to the model (for the step summary)."""
    role = message.get("role", "?")
    if role == "tool":
        return f"tool result: {message.get('content', '')}"
    return f"{role}: {message.get('content') or ''}"


def _describe_response(resp: LLMResponse) -> str:
    if resp.tool_calls:
        calls = ", ".join(
            f"{c.name}({c.raw_arguments if c.arguments_error else json.dumps(c.arguments)})"
            for c in resp.tool_calls
        )
        source = " (parsed from text)" if resp.tool_calls_from_text else ""
        return f"tool calls{source}: {calls}"
    return resp.content or ""


def _call_arguments_text(call: dict[str, Any]) -> str:
    """What the model sent as arguments, including invalid JSON, for the step record."""
    raw = call.get("x_raw_arguments") or call["function"].get("arguments")
    return str(raw) if raw else "{}"


def pending_tool_calls(messages: list[Message]) -> list[dict[str, Any]]:
    """Tool calls in the last assistant message that have no tool result yet, in order."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "assistant":
            answered = {
                m.get("tool_call_id") for m in messages[index + 1 :] if m.get("role") == "tool"
            }
            calls: list[dict[str, Any]] = messages[index].get("tool_calls") or []
            return [c for c in calls if c["id"] not in answered]
    return []


class AgentRunner:
    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        llm: ChatModel | None = None,
        settings: Settings | None = None,
        tools: ToolRegistry = registry,
    ) -> None:
        self.session_factory = session_factory
        self._llm = llm
        self.settings = settings or get_settings()
        self.tools = tools

    @property
    def llm(self) -> ChatModel:
        if self._llm is None:
            self._llm = TokenFactoryClient(self.settings)
        return self._llm

    async def handle_job(self, _sf: SessionFactory, payload: dict[str, Any]) -> None:
        await self.run(uuid.UUID(payload["run_id"]))

    async def run(self, run_id: uuid.UUID) -> None:
        from app.agents.registry import get_agent

        async with self.session_factory() as session:
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None:
                logger.warning("run not found", extra={"run_id": str(run_id)})
                return
            if run.status not in (RunStatus.queued, RunStatus.running):
                # Already finished, or still waiting for a decision. Nothing to do.
                logger.info("run skipped", extra={"run_id": str(run_id), "status": run.status})
                await session.rollback()
                return
            run.status = RunStatus.running
            run.stop_reason = None
            run.started_at = run.started_at or _now()
            await session.commit()

            try:
                agent = get_agent(run.agent)
                reason = await self._loop(session, run, agent)
            except RunStoppedError:
                await session.rollback()
                logger.info("run stopped by user", extra={"run_id": str(run_id)})
                return
            except BudgetExceededError as exc:
                await session.rollback()
                await session.refresh(run)
                await record_step(session, run, kind="budget", output_summary=str(exc))
                await self._finish(session, run, RunStatus.stopped, StopReason.budget)
                return
            except Exception as exc:
                logger.exception("run failed", extra={"run_id": str(run_id)})
                await session.rollback()
                await session.refresh(run)
                error = summarize(f"{type(exc).__name__}: {exc}", 1000)
                await record_step(session, run, kind="error", output_summary=error)
                await self._finish(session, run, RunStatus.failed, StopReason.error, error=error)
                return

            if reason == StopReason.waiting_approval:
                return  # the policy engine already set the status and committed
            status = RunStatus.succeeded if reason == StopReason.completed else RunStatus.stopped
            await self._finish(session, run, status, reason)

    async def _finish(
        self,
        session: AsyncSession,
        run: Run,
        status: RunStatus,
        reason: StopReason,
        *,
        error: str | None = None,
    ) -> None:
        current = await session.scalar(select(Run.status).where(Run.id == run.id).with_for_update())
        if current in TERMINAL_RUN_STATUSES:
            # Stopped by a user meanwhile. Keep their status.
            await session.rollback()
            return
        run.status = status
        run.stop_reason = reason
        run.error = error
        if status in TERMINAL_RUN_STATUSES:
            run.finished_at = _now()
        await session.commit()
        logger.info(
            "run finished",
            extra={"run_id": str(run.id), "status": status, "stop_reason": reason},
        )

    async def _loop(self, session: AsyncSession, run: Run, agent: Agent) -> StopReason:
        if not run.messages:
            run.messages = [
                {"role": "system", "content": agent.system_prompt},
                {"role": "user", "content": run.goal},
            ]
            await session.commit()
        schemas = [self.tools.get(name).openai_schema() for name in agent.tools]

        while True:
            if await self._handle_pending(session, run, agent):
                return StopReason.waiting_approval
            await self._check_not_stopped(session, run)

            llm_calls = await session.scalar(
                select(func.count(RunStep.id)).where(
                    RunStep.run_id == run.id, RunStep.kind == "llm"
                )
            )
            if (llm_calls or 0) >= self.settings.max_steps_per_run:
                return StopReason.max_steps

            target = route(agent.task_type, self.settings)
            max_tokens = await check_budget(
                session,
                run,
                target.tier,
                self.settings,
                prompt_tokens=estimate_prompt_tokens(run.messages, schemas),
            )
            await session.commit()  # no transaction open during the model call
            resp = await self.llm.chat(
                model_id=target.model_id,
                messages=run.messages,
                tools=schemas,
                max_tokens=max_tokens,
                reasoning=target.reasoning,
            )

            cost = estimate_cost(
                target.tier, resp.prompt_tokens, resp.completion_tokens, self.settings
            )
            run.prompt_tokens += resp.prompt_tokens
            run.completion_tokens += resp.completion_tokens
            if cost is not None:
                run.cost_usd = (run.cost_usd or 0) + cost
            await record_step(
                session,
                run,
                kind="llm",
                agent=agent.name,
                model_tier=target.tier,
                model_id=resp.model_id,
                input_summary=f"{len(run.messages)} messages; last {_describe(run.messages[-1])}",
                output_summary=_describe_response(resp),
                prompt_tokens=resp.prompt_tokens,
                completion_tokens=resp.completion_tokens,
                latency_ms=resp.latency_ms,
                cost_usd=cost,
            )
            # Reassign (not append) so SQLAlchemy sees the JSONB change.
            assistant = _assistant_message(resp, _used_tool_call_ids(run.messages))
            run.messages = [*run.messages, assistant]
            await session.commit()

            if not resp.tool_calls:
                if resp.truncated:
                    # Tokens are recorded above. A cut-off answer is not a result.
                    if resp.content:
                        run.result = redact(resp.content)
                        await session.commit()
                        raise LLMError(
                            f"The answer was cut off at max_tokens={max_tokens}. "
                            "Raise LLM_MAX_TOKENS. The partial answer is in the run result."
                        )
                    raise LLMError(
                        f"{resp.model_id} hit max_tokens={max_tokens} before answering "
                        "(reasoning used the budget). Raise LLM_MAX_TOKENS."
                    )
                run.result = redact(resp.content) if resp.content else None
                return StopReason.completed

    @staticmethod
    async def _check_not_stopped(session: AsyncSession, run: Run) -> None:
        current = await session.scalar(select(Run.status).where(Run.id == run.id))
        if current in TERMINAL_RUN_STATUSES:
            raise RunStoppedError(str(run.id))

    async def _handle_pending(self, session: AsyncSession, run: Run, agent: Agent) -> bool:
        """Run pending tool calls in order. Returns True when the run must wait for approval."""
        for call in pending_tool_calls(run.messages):
            name: str = call["function"]["name"]
            await self._check_not_stopped(session, run)
            await session.commit()  # no transaction open while the tool runs
            start = time.perf_counter()
            result = await self._run_tool_call(session, run, agent, call)
            if result is None:
                return True
            await record_step(
                session,
                run,
                # A rejected call never ran, so it is not recorded as a tool step.
                kind="tool_rejected" if result.rejected else "tool",
                agent=agent.name,
                tool_name=name,
                input_summary=f"{name}({_call_arguments_text(call)})",
                output_summary=result.text,
                latency_ms=None if result.rejected else round((time.perf_counter() - start) * 1000),
            )
            message = {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": summarize(result.text, MAX_TOOL_RESULT_CHARS),
            }
            run.messages = [*run.messages, message]
            await session.commit()
        return False

    async def _run_tool_call(
        self, session: AsyncSession, run: Run, agent: Agent, call: dict[str, Any]
    ) -> ToolResult | None:
        """The tool result to send back to the model, or None when waiting for approval."""
        name: str = call["function"]["name"]
        if name not in agent.tools:
            return ToolResult(
                f"error: tool {name!r} is not available. Available: {', '.join(agent.tools)}"
            )
        try:
            spec = self.tools.get(name)
        except ToolNotFoundError:
            return ToolResult(f"error: tool {name!r} does not exist")

        if call.get("x_arguments_error"):
            return ToolResult(
                f"error: {call['x_arguments_error']}. Send the arguments as a JSON object."
            )
        raw_args, args_error = parse_arguments(call["function"].get("arguments"))
        if args_error:
            return ToolResult(f"error: {args_error}")
        try:
            args = spec.args_model.model_validate(raw_args)
        except ValidationError as exc:
            return ToolResult(
                f"error: invalid arguments for {name}: {exc.errors(include_url=False)}"
            )

        try:
            outcome = await handle_tool_call(session, run, spec, args, call["id"])
        except PolicyViolationError:
            raise  # a bug, not a tool failure: stop the run
        except Exception as exc:
            logger.warning("tool failed", extra={"tool": name, "error": type(exc).__name__})
            return ToolResult(f"error: {name} failed: {type(exc).__name__}: {exc}")

        match outcome:
            case AwaitingApproval():
                return None
            case Rejected(approval=approval):
                # The approved/rejected arguments are the stored ones, not the model's latest.
                return ToolResult(rejected_message(name, approval.arguments), rejected=True)
            case Executed(output=output):
                return ToolResult(output)


async def run_agent_job(session_factory: SessionFactory, payload: dict[str, Any]) -> None:
    """Worker handler for "run_agent" and "resume_run" jobs."""
    await AgentRunner(session_factory).handle_job(session_factory, payload)
