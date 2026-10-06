"""Scripted stand-in for the Token Factory client. Tests never call the real API."""

import copy
import uuid
from collections.abc import Sequence
from typing import Any

from app.llm.client import LLMError, LLMResponse, Message, ToolSchema
from app.llm.tool_calls import ToolCall


def tool_call(tool_name: str, /, **arguments: Any) -> ToolCall:
    return ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name=tool_name, arguments=arguments)


def reply(
    content: str | None = None,
    *calls: ToolCall,
    prompt_tokens: int = 100,
    completion_tokens: int = 20,
    finish_reason: str | None = None,
) -> LLMResponse:
    return LLMResponse(
        model_id="test/nano",
        content=content,
        tool_calls=list(calls),
        finish_reason=finish_reason or ("tool_calls" if calls else "stop"),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=5,
    )


class FakeLLM:
    def __init__(self, *responses: LLMResponse | Exception, repeat_last: bool = False) -> None:
        self.responses = list(responses)
        self.repeat_last = repeat_last
        self.requests: list[list[Message]] = []
        self.max_tokens: list[int | None] = []
        self.reasoning: list[bool] = []

    async def chat(
        self,
        *,
        model_id: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] = (),
        max_tokens: int | None = None,
        reasoning: bool = True,
    ) -> LLMResponse:
        self.requests.append(copy.deepcopy(list(messages)))
        self.max_tokens.append(max_tokens)
        self.reasoning.append(reasoning)
        if not self.responses:
            raise LLMError("FakeLLM has no scripted responses left")
        item = self.responses[0] if self.repeat_last and len(self.responses) == 1 else None
        item = item or self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if self.repeat_last:
            # Fresh tool call ids each time, like a real model.
            item = item.model_copy(
                update={
                    "tool_calls": [
                        c.model_copy(update={"id": f"call_{uuid.uuid4().hex[:8]}"})
                        for c in item.tool_calls
                    ]
                }
            )
        return item

    def tool_results(self, request_index: int = -1) -> list[str]:
        return [m["content"] for m in self.requests[request_index] if m.get("role") == "tool"]
