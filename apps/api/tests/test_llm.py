"""TokenFactory over a mocked HTTP transport: what is sent on the wire and how replies parse.

Nothing reaches Token Factory: httpx2.MockTransport answers every request.
"""

import json
from typing import Any

import httpx
import httpx2
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.llm.budget import MemoryUsageStore, RunBudget
from app.llm.client import LLMError, TokenFactory, wire_messages
from app.llm.router import Task
from app.llm.tool_calls import parse_text_tool_calls

pytestmark = pytest.mark.anyio

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
SECRET = "sk-test-secret-key-1234567890"  # noqa: S105
MESSAGES = [{"role": "user", "content": "List the services"}]


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "token_factory_base_url": "http://token-factory.invalid/v1",
        "token_factory_api_key": SecretStr(SECRET),
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


def _client(
    responses: list[httpx2.Response], seen: list[httpx2.Request], **overrides: Any
) -> TokenFactory:
    queue = list(responses)

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return queue.pop(0)

    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return TokenFactory(_settings(**overrides), http_client=http_client, retry_base_seconds=0)


def _budget(**overrides: Any) -> RunBudget:
    return RunBudget(usage=MemoryUsageStore(), settings=_settings(**overrides))


async def test_native_tool_call_and_wire_format() -> None:
    seen: list[httpx2.Request] = []
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "restart_fake_service", "arguments": '{"name": "api"}'},
    }
    body = _completion({"content": None, "tool_calls": [call]}, "tool_calls")
    client = _client([httpx2.Response(200, json=body)], seen)
    resp = await client.chat(Task.tool_step, MESSAGES, budget=_budget(), tools=TOOLS)

    assert [(c.id, c.name, c.arguments) for c in resp.tool_calls] == [
        ("call_1", "restart_fake_service", {"name": "api"})
    ]
    assert (resp.prompt_tokens, resp.completion_tokens) == (50, 7)
    sent = json.loads(seen[0].content)
    assert sent["model"] == "m/super"  # tool_step routes to Super
    assert sent["max_tokens"] == 1500  # the route's cap
    assert (sent["temperature"], sent["top_p"]) == (1.0, 0.95)  # NVIDIA's Nemotron 3 sampling
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}
    assert sent["tool_choice"] == "auto" and sent["tools"]


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        (Task.summarize_graph, {"enable_thinking": False}),
        (Task.detect_app_spec, {"enable_thinking": True, "low_effort": True}),
        (Task.fix_build, {"enable_thinking": True}),
    ],
)
async def test_reasoning_mode_on_the_wire(task: Task, expected: dict[str, Any]) -> None:
    seen: list[httpx2.Request] = []
    client = _client([httpx2.Response(200, json=_completion({"content": "ok"}))], seen)
    resp = await client.chat(task, MESSAGES, budget=_budget())
    assert json.loads(seen[0].content)["chat_template_kwargs"] == expected
    assert resp.reasoning_mode is not None


async def test_text_tool_call_fallback() -> None:
    seen: list[httpx2.Request] = []
    content = (
        "<think>I should restart it.</think>\n"
        '<tool_call>{"name": "restart_fake_service", "arguments": {"name": "api"}}</tool_call>'
    )
    client = _client([httpx2.Response(200, json=_completion({"content": content}))], seen)
    resp = await client.chat(Task.summarize, MESSAGES, budget=_budget(), tools=TOOLS)
    assert resp.tool_calls_from_text
    assert [(c.name, c.arguments) for c in resp.tool_calls] == [
        ("restart_fake_service", {"name": "api"})
    ]
    assert resp.content is None
    assert resp.reasoning == "I should restart it."


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


@pytest.mark.parametrize(
    ("message", "trace"),
    [
        ({"content": "<think>hmm</think>All done."}, "hmm"),
        ({"content": "All done.", "reasoning_content": "hmm"}, "hmm"),
        ({"content": "All done.", "reasoning": "hmm"}, "hmm"),
        ({"content": "hmm</think>All done."}, "hmm"),  # template opened the think block
    ],
)
async def test_reasoning_trace_captured_from_any_field(message: dict[str, Any], trace: str) -> None:
    seen: list[httpx2.Request] = []
    client = _client([httpx2.Response(200, json=_completion(message))], seen)
    resp = await client.chat(Task.summarize, MESSAGES, budget=_budget())
    assert resp.content == "All done."
    assert resp.reasoning == trace


async def test_retries_429_with_backoff_then_succeeds() -> None:
    seen: list[httpx2.Request] = []
    client = _client(
        [
            httpx2.Response(429, json={"error": {"message": "slow down"}}),
            httpx2.Response(500, json={"error": {"message": "oops"}}),
            httpx2.Response(200, json=_completion({"content": "ok"})),
        ],
        seen,
    )
    resp = await client.chat(Task.summarize, MESSAGES, budget=_budget())
    assert resp.content == "ok" and resp.attempts == 3 and len(seen) == 3


async def test_gives_up_after_max_attempts_and_redacts() -> None:
    seen: list[httpx2.Request] = []
    error = {"error": {"message": f"bad key {SECRET}"}}
    client = _client([httpx2.Response(503, json=error) for _ in range(3)], seen)
    with pytest.raises(LLMError) as exc_info:
        await client.chat(Task.summarize, MESSAGES, budget=_budget())
    assert len(seen) == 3  # LLM_MAX_ATTEMPTS, no fallback on this route
    assert SECRET not in str(exc_info.value)
    assert "List the services" not in str(exc_info.value)


async def test_no_retry_and_no_fallback_on_400() -> None:
    seen: list[httpx2.Request] = []
    client = _client([httpx2.Response(400, json={"error": {"message": "bad"}})], seen)
    with pytest.raises(LLMError):
        # detect_app_spec has a fallback tier, but a 400 is not worth retrying anywhere.
        await client.chat(Task.detect_app_spec, MESSAGES, budget=_budget())
    assert len(seen) == 1


async def test_length_is_returned_with_usage_not_raised() -> None:
    # The caller records the tokens, then decides what a cut-off response means.
    seen: list[httpx2.Request] = []
    body = _completion({"content": ""}, "length")
    client = _client([httpx2.Response(200, json=body)], seen)
    resp = await client.chat(Task.summarize, MESSAGES, budget=_budget())
    assert resp.truncated and resp.content is None and resp.completion_tokens > 0


async def test_invalid_native_arguments_are_kept_raw() -> None:
    seen: list[httpx2.Request] = []
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "restart_fake_service", "arguments": '{"name": "ap'},
    }
    body = _completion({"content": None, "tool_calls": [call]}, "length")
    client = _client([httpx2.Response(200, json=body)], seen)
    resp = await client.chat(Task.summarize, MESSAGES, budget=_budget(), tools=TOOLS)
    [parsed] = resp.tool_calls
    assert parsed.arguments == {} and parsed.arguments_error
    assert parsed.raw_arguments == '{"name": "ap'


def test_private_keys_and_reasoning_are_not_sent_to_the_provider() -> None:
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": "ok",
            "reasoning_content": "secret thoughts",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "x", "arguments": "{}"},
                    "x_arguments_error": "bad",
                    "x_raw_arguments": "{oops",
                }
            ],
        }
    ]
    [wire] = wire_messages(messages)
    assert "reasoning_content" not in wire
    assert wire["tool_calls"] == [
        {"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}
    ]
    assert "x_raw_arguments" in messages[0]["tool_calls"][0]  # the stored copy is unchanged


async def test_missing_config_is_a_clear_error() -> None:
    with pytest.raises(LLMError, match="TOKEN_FACTORY_API_KEY"):
        TokenFactory(_settings(token_factory_api_key=None))


async def test_rerank_posts_documented_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.2, "document": {"text": "b"}},
                    {"index": 0, "relevance_score": 0.9, "document": {"text": "a"}},
                ],
                "usage": {"total_tokens": 40},
            },
        )

    client = TokenFactory(
        _settings(model_rerank="r/model", price_rerank_per_m=0.05),
        rerank_http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    usage = MemoryUsageStore()
    ranked = await client.rerank("db cpu spike", ["a", "b"], usage=usage)
    assert ranked == [(0, 0.9), (1, 0.2)]
    [request] = seen
    assert str(request.url) == "http://token-factory.invalid/v1/rerank"
    assert json.loads(request.content) == {
        "model": "r/model",
        "query": "db cpu spike",
        "documents": ["a", "b"],
    }
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    assert usage.tokens_today == 40
