"""LLM layer tests. HTTP is mocked at the transport level: nothing reaches Token Factory."""

import json
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.db.models import Run, RunStep, Workspace
from app.db.session import SessionFactory
from app.llm.budget import BudgetExceededError, check_budget
from app.llm.client import LLMError, TokenFactoryClient
from app.llm.costs import estimate_cost
from app.llm.router import ModelNotConfiguredError, TaskType, Tier, route
from app.llm.tool_calls import parse_text_tool_calls

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "restart_fake_service",
            "description": "Restart",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}},
        },
    }
]


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "token_factory_base_url": "http://token-factory.invalid/v1",
        "token_factory_api_key": SecretStr("sk-test-secret-key-1234567890"),
        "model_nano": "m/nano",
        "model_super": "m/super",
        "model_ultra": "m/ultra",
        **overrides,
    }
    return Settings(_env_file=None, **values)


def _completion(message: dict[str, Any], finish_reason: str = "stop") -> dict[str, Any]:
    return {
        "id": "cmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "m/nano",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", **message},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 7, "total_tokens": 57},
    }


def _client(responses: list[httpx2.Response], seen: list[httpx2.Request]) -> TokenFactoryClient:
    queue = list(responses)

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return queue.pop(0)

    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return TokenFactoryClient(_settings(), http_client=http_client, retry_delay_seconds=0)


MESSAGES = [{"role": "user", "content": "List the services"}]


@pytest.mark.anyio
async def test_native_tool_call() -> None:
    seen: list[httpx2.Request] = []
    body = _completion(
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "restart_fake_service", "arguments": '{"name": "api"}'},
                }
            ],
        },
        "tool_calls",
    )
    client = _client([httpx2.Response(200, json=body)], seen)
    resp = await client.chat(model_id="m/nano", messages=MESSAGES, tools=TOOLS)

    assert [(c.id, c.name, c.arguments) for c in resp.tool_calls] == [
        ("call_1", "restart_fake_service", {"name": "api"})
    ]
    assert not resp.tool_calls_from_text
    assert (resp.prompt_tokens, resp.completion_tokens) == (50, 7)
    sent = json.loads(seen[0].content)
    assert sent["model"] == "m/nano" and sent["tools"] and sent["max_tokens"] == 4096


@pytest.mark.anyio
async def test_text_tool_call_fallback() -> None:
    seen: list[httpx2.Request] = []
    content = (
        "<think>I should restart it.</think>\n"
        '<tool_call>{"name": "restart_fake_service", "arguments": {"name": "api"}}</tool_call>'
    )
    client = _client([httpx2.Response(200, json=_completion({"content": content}))], seen)
    resp = await client.chat(model_id="m/nano", messages=MESSAGES, tools=TOOLS)

    assert resp.tool_calls_from_text
    assert [(c.name, c.arguments) for c in resp.tool_calls] == [
        ("restart_fake_service", {"name": "api"})
    ]
    assert resp.content is None


@pytest.mark.parametrize(
    "content",
    [
        '{"name": "restart_fake_service", "arguments": {"name": "api"}}',
        '```json\n{"tool": "restart_fake_service", "args": {"name": "api"}}\n```',
        'Calling: {"tool_calls": [{"function": {"name": "restart_fake_service", '
        '"arguments": "{\\"name\\": \\"api\\"}"}}]}',
        '[{"name": "restart_fake_service", "parameters": {"name": "api"}}]',
    ],
)
def test_text_tool_call_shapes(content: str) -> None:
    calls = parse_text_tool_calls(content, ["restart_fake_service"])
    assert [(c.name, c.arguments) for c in calls] == [("restart_fake_service", {"name": "api"})]


def test_text_fallback_ignores_unknown_tools_and_plain_json() -> None:
    assert parse_text_tool_calls('{"name": "rm_rf", "arguments": {}}', ["restart"]) == []
    assert parse_text_tool_calls('The config is {"replicas": 3}.', ["restart"]) == []


@pytest.mark.anyio
async def test_plain_answer_strips_reasoning() -> None:
    seen: list[httpx2.Request] = []
    body = _completion({"content": "<think>hmm</think>All done."})
    client = _client([httpx2.Response(200, json=body)], seen)
    resp = await client.chat(model_id="m/nano", messages=MESSAGES, tools=TOOLS)
    assert resp.content == "All done." and resp.tool_calls == []


@pytest.mark.anyio
async def test_retries_once_on_429_then_succeeds() -> None:
    seen: list[httpx2.Request] = []
    client = _client(
        [
            httpx2.Response(429, json={"error": {"message": "slow down"}}),
            httpx2.Response(200, json=_completion({"content": "ok"})),
        ],
        seen,
    )
    resp = await client.chat(model_id="m/nano", messages=MESSAGES)
    assert resp.content == "ok" and len(seen) == 2


@pytest.mark.anyio
async def test_gives_up_after_one_retry_and_redacts() -> None:
    seen: list[httpx2.Request] = []
    error = {"error": {"message": "bad key sk-test-secret-key-1234567890"}}
    client = _client([httpx2.Response(503, json=error), httpx2.Response(503, json=error)], seen)
    with pytest.raises(LLMError) as exc_info:
        await client.chat(model_id="m/nano", messages=MESSAGES)
    assert len(seen) == 2
    assert "sk-test-secret-key-1234567890" not in str(exc_info.value)
    assert "List the services" not in str(exc_info.value)


@pytest.mark.anyio
async def test_no_retry_on_400() -> None:
    seen: list[httpx2.Request] = []
    client = _client([httpx2.Response(400, json={"error": {"message": "bad"}})], seen)
    with pytest.raises(LLMError):
        await client.chat(model_id="m/nano", messages=MESSAGES)
    assert len(seen) == 1


@pytest.mark.anyio
async def test_reasoning_used_all_tokens_is_a_clear_error() -> None:
    seen: list[httpx2.Request] = []
    body = _completion({"content": ""}, "length")
    client = _client([httpx2.Response(200, json=body)], seen)
    with pytest.raises(LLMError, match="LLM_MAX_TOKENS"):
        await client.chat(model_id="m/nano", messages=MESSAGES)


def test_router_defaults_and_no_ultra() -> None:
    settings = _settings()
    assert route(TaskType.classify, settings).tier == Tier.nano
    assert route(TaskType.plan, settings).model_id == "m/super"
    assert route(TaskType.root_cause, settings).tier == Tier.ultra
    downgraded = route(TaskType.root_cause, _settings(dev_mode_no_ultra=True))
    assert (downgraded.tier, downgraded.model_id) == (Tier.super, "m/super")
    with pytest.raises(ModelNotConfiguredError, match="MODEL_NANO"):
        route(TaskType.detect, _settings(model_nano=None))


def test_cost_estimate() -> None:
    priced = _settings(price_nano_input_per_m=0.06, price_nano_output_per_m=0.24)
    assert estimate_cost(Tier.nano, 1_000_000, 500_000, priced) == Decimal("0.180000")
    assert estimate_cost(Tier.super, 1000, 1000, priced) is None
    assert estimate_cost(Tier.nano, 1000, 1000, _settings(price_nano_input_per_m=0.06)) is None


async def _run_with_steps(
    sf: SessionFactory, ws: Workspace, tokens: int, tier: Tier = Tier.nano
) -> Run:
    async with sf() as session:
        run = Run(workspace_id=ws.id, agent="echo_ops", goal="g", prompt_tokens=tokens)
        session.add(run)
        await session.flush()
        session.add(
            RunStep(
                run_id=run.id,
                seq=1,
                agent="echo_ops",
                kind="llm",
                model_tier=tier,
                prompt_tokens=tokens,
                completion_tokens=0,
            )
        )
        await session.commit()
        return run


@pytest.mark.anyio
async def test_budget_checks(session_factory: SessionFactory, workspace: Workspace) -> None:
    run = await _run_with_steps(session_factory, workspace, 900, Tier.ultra)
    async with session_factory() as session:
        await check_budget(session, run, Tier.nano, _settings(max_tokens_per_run=1000))
        with pytest.raises(BudgetExceededError, match="MAX_TOKENS_PER_RUN"):
            await check_budget(session, run, Tier.nano, _settings(max_tokens_per_run=900))
        with pytest.raises(BudgetExceededError, match="DAILY_TOKEN_BUDGET"):
            await check_budget(session, run, Tier.nano, _settings(daily_token_budget=500))
        with pytest.raises(BudgetExceededError, match="MAX_ULTRA_CALLS"):
            await check_budget(session, run, Tier.ultra, _settings(max_ultra_calls_per_incident=1))
        await check_budget(session, run, Tier.nano, _settings(max_ultra_calls_per_incident=1))
