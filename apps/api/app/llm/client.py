"""Async OpenAI-compatible client for Nebius Token Factory.

Base URL, key, and model IDs come only from settings. One retry on 429/5xx with jitter.
Never logs the API key or prompts: only model ID, tokens, latency, and attempt.
"""

import asyncio
import logging
import random
import time
from collections.abc import Sequence
from typing import Any, Protocol, cast

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.llm.tool_calls import ToolCall, parse_arguments, parse_text_tool_calls, strip_reasoning
from app.security.redact import summarize

logger = logging.getLogger("groundcrew.llm")

Message = dict[str, Any]
ToolSchema = dict[str, Any]


class LLMError(RuntimeError):
    """The call failed. The message is redacted and never includes the prompt."""


class LLMConfigError(LLMError):
    pass


class LLMResponse(BaseModel):
    model_id: str
    content: str | None
    tool_calls: list[ToolCall]
    # True when tool calls were parsed from text, not returned natively.
    tool_calls_from_text: bool = False
    finish_reason: str | None
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


class ChatModel(Protocol):
    """What the agent loop needs. Tests pass a fake that implements this."""

    async def chat(
        self,
        *,
        model_id: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] = (),
        max_tokens: int | None = None,
    ) -> LLMResponse: ...


def _is_retryable(exc: openai.OpenAIError) -> bool:
    return isinstance(exc, openai.RateLimitError) or (
        isinstance(exc, openai.APIStatusError) and exc.status_code >= 500
    )


class TokenFactoryClient:
    MAX_ATTEMPTS = 2  # first try + one retry

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        http_client: Any = None,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        settings = settings or get_settings()
        if not settings.token_factory_base_url or settings.token_factory_api_key is None:
            raise LLMConfigError(
                "TOKEN_FACTORY_BASE_URL and TOKEN_FACTORY_API_KEY must be set. See .env.example."
            )
        self._settings = settings
        self._retry_delay = retry_delay_seconds
        self._client = AsyncOpenAI(
            base_url=settings.token_factory_base_url,
            api_key=settings.token_factory_api_key.get_secret_value(),
            timeout=settings.llm_timeout_seconds,
            max_retries=0,  # we retry ourselves, once, only on 429/5xx
            http_client=http_client,
        )

    async def chat(
        self,
        *,
        model_id: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] = (),
        max_tokens: int | None = None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": list(messages),
            "max_tokens": max_tokens or self._settings.llm_max_tokens,
        }
        if tools:
            kwargs["tools"] = list(tools)
            kwargs["tool_choice"] = "auto"

        start = time.perf_counter()
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                completion = await self._client.chat.completions.create(**kwargs)
                break
            except openai.OpenAIError as exc:
                if attempt < self.MAX_ATTEMPTS and _is_retryable(exc):
                    delay = self._retry_delay * random.uniform(0.5, 1.5)  # noqa: S311
                    logger.warning(
                        "llm call retrying",
                        extra={"model_id": model_id, "error": type(exc).__name__},
                    )
                    await asyncio.sleep(delay)
                    continue
                message = summarize(f"{type(exc).__name__}: {exc}", 500)
                raise LLMError(f"Token Factory call to {model_id} failed: {message}") from None
        latency_ms = round((time.perf_counter() - start) * 1000)

        response = self._parse(model_id, completion, latency_ms, tools)
        logger.info(
            "llm call",
            extra={
                "model_id": model_id,
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "latency_ms": latency_ms,
                "attempts": attempt,
                "tool_calls": len(response.tool_calls),
                "finish_reason": response.finish_reason,
            },
        )
        return response

    @staticmethod
    def _parse(
        model_id: str, completion: Any, latency_ms: int, tools: Sequence[ToolSchema]
    ) -> LLMResponse:
        usage = completion.usage
        prompt_tokens = int(usage.prompt_tokens) if usage else 0
        completion_tokens = int(usage.completion_tokens) if usage else 0
        if not completion.choices:
            raise LLMError(f"Token Factory returned no choices for {model_id}.")
        choice = completion.choices[0]
        message = choice.message
        content = strip_reasoning(message.content) if message.content else None

        tool_calls = []
        for call in message.tool_calls or []:
            function = cast(Any, call).function
            arguments, error = parse_arguments(function.arguments)
            tool_calls.append(
                ToolCall(id=call.id, name=function.name, arguments=arguments, arguments_error=error)
            )

        from_text = False
        if not tool_calls and content and tools:
            names = [t["function"]["name"] for t in tools]
            tool_calls = parse_text_tool_calls(content, names)
            from_text = bool(tool_calls)
            if from_text:
                content = None

        if not content and not tool_calls and choice.finish_reason == "length":
            # Nemotron reasons before answering and can spend every token on it.
            raise LLMError(
                f"{model_id} hit max_tokens before answering (reasoning used the budget). "
                "Raise LLM_MAX_TOKENS."
            )

        return LLMResponse(
            model_id=model_id,
            content=content or None,
            tool_calls=tool_calls,
            tool_calls_from_text=from_text,
            finish_reason=choice.finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
        )
