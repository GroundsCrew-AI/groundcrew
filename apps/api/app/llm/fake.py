"""FakeLLM: the real TokenFactory with the network replaced by scripted replies.

Routing, budgets, retries, fallback, schema repair, reasoning capture, cost, and the on_call
hook all run the same code as in production; only the HTTP calls are scripted. Unit tests
use this and never reach Token Factory.

    llm = FakeLLM(
        reply(None, tool_call("list_fake_services")),   # a tool call
        reply('{"port": 3000'),                          # invalid JSON, triggers a repair
        status_error(503),                               # a 5xx, triggers retry/fallback
        reply("done", reasoning="thinking..."),          # answer plus a reasoning trace
    )
"""

import copy
import dataclasses
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx2
import openai
from openai.types.chat import ChatCompletion
from openai.types.create_embedding_response import CreateEmbeddingResponse

from app.config import Settings
from app.llm.client import OnCall, TokenFactory
from app.llm.tool_calls import ToolCall

_REQUEST = httpx2.Request("POST", "http://token-factory.fake/v1/chat/completions")


@dataclass
class FakeReply:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning: str | None = None
    # Where the trace comes back: "reasoning_content", "reasoning", or "inline" (<think> tags).
    reasoning_field: str = "reasoning_content"
    prompt_tokens: int = 100
    completion_tokens: int = 20
    finish_reason: str | None = None

    def completion(self, model: str) -> ChatCompletion:
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.reasoning and self.reasoning_field == "inline":
            message["content"] = f"<think>{self.reasoning}</think>{self.content or ''}"
        elif self.reasoning:
            message[self.reasoning_field] = self.reasoning
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.raw_arguments
                        if call.arguments_error
                        else json.dumps(call.arguments),
                    },
                }
                for call in self.tool_calls
            ]
        finish = self.finish_reason or ("tool_calls" if self.tool_calls else "stop")
        return ChatCompletion.model_validate(
            {
                "id": f"cmpl-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion",
                "created": 0,
                "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {
                    "prompt_tokens": self.prompt_tokens,
                    "completion_tokens": self.completion_tokens,
                    "total_tokens": self.prompt_tokens + self.completion_tokens,
                },
            }
        )


def tool_call(tool_name: str, /, **arguments: Any) -> ToolCall:
    return ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name=tool_name, arguments=arguments)


def reply(
    content: str | None = None,
    *calls: ToolCall,
    prompt_tokens: int = 100,
    completion_tokens: int = 20,
    finish_reason: str | None = None,
    reasoning: str | None = None,
    reasoning_field: str = "reasoning_content",
) -> FakeReply:
    return FakeReply(
        content=content,
        tool_calls=list(calls),
        reasoning=reasoning,
        reasoning_field=reasoning_field,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        finish_reason=finish_reason,
    )


def status_error(status: int, message: str = "fake error") -> openai.APIStatusError:
    """The exception the OpenAI SDK raises for an HTTP error status (429, 5xx, 4xx)."""
    response = httpx2.Response(status, request=_REQUEST)
    if status == 429:
        return openai.RateLimitError(message, response=response, body=None)
    if status >= 500:
        return openai.InternalServerError(message, response=response, body=None)
    return openai.APIStatusError(message, response=response, body=None)


def timeout_error() -> openai.APITimeoutError:
    return openai.APITimeoutError(request=_REQUEST)


Scripted = FakeReply | BaseException


class FakeLLM(TokenFactory):
    def __init__(
        self,
        *script: Scripted,
        repeat_last: bool = False,
        settings: Settings | None = None,
        on_call: OnCall | None = None,
        embeddings: Sequence[list[list[float]]] = (),
        rerank_results: Sequence[dict[str, Any]] = (),
    ) -> None:
        self.script: list[Scripted] = list(script)
        self.repeat_last = repeat_last
        # Every request body the client sent (the messages are deep copies).
        self.calls: list[dict[str, Any]] = []
        self.embedding_script = list(embeddings)
        self.rerank_script = list(rerank_results)
        super().__init__(settings, on_call=on_call, retry_base_seconds=0)

    def _connect(self, http_client: Any) -> None:
        self._oai = None  # no network, no key needed

    async def _create(self, kwargs: dict[str, Any]) -> Any:
        self.calls.append(copy.deepcopy(kwargs))
        if not self.script:
            raise RuntimeError("FakeLLM has no scripted replies left")
        if self.repeat_last and len(self.script) == 1:
            item = self.script[0]
            if isinstance(item, FakeReply):
                # Fresh tool call ids each time, like a real model.
                item = dataclasses.replace(
                    item,
                    tool_calls=[
                        c.model_copy(update={"id": f"call_{uuid.uuid4().hex[:8]}"})
                        for c in item.tool_calls
                    ],
                )
        else:
            item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item.completion(kwargs["model"])

    async def _create_embeddings(self, kwargs: dict[str, Any]) -> Any:
        self.calls.append(copy.deepcopy(kwargs))
        vectors = self.embedding_script.pop(0)
        return CreateEmbeddingResponse.model_validate(
            {
                "object": "list",
                "model": kwargs["model"],
                "data": [
                    {"object": "embedding", "index": i, "embedding": v}
                    for i, v in enumerate(vectors)
                ],
                "usage": {"prompt_tokens": 8 * len(vectors), "total_tokens": 8 * len(vectors)},
            }
        )

    async def _post_rerank(self, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(copy.deepcopy(body))
        return self.rerank_script.pop(0)

    # ------------------------------------------------------------- assertions

    @property
    def requests(self) -> list[list[dict[str, Any]]]:
        """The messages of each chat request, as sent to the provider."""
        return [c["messages"] for c in self.calls if "messages" in c]

    @property
    def models(self) -> list[str]:
        return [c["model"] for c in self.calls if "messages" in c]

    @property
    def max_tokens(self) -> list[int]:
        return [c["max_tokens"] for c in self.calls if "messages" in c]

    @property
    def reasoning_kwargs(self) -> list[dict[str, Any]]:
        return [c["extra_body"]["chat_template_kwargs"] for c in self.calls if "messages" in c]

    def tool_results(self, request_index: int = -1) -> list[str]:
        return [m["content"] for m in self.requests[request_index] if m.get("role") == "tool"]
