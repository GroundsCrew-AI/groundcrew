"""The only place in Groundcrew that talks to Nebius Token Factory.

Every model call goes through `TokenFactory.chat(task, ...)`: routing (tier + reasoning mode),
budgets, retries, fallback, schema output with one repair, reasoning capture, cost, and the
on_call hook all happen here. Base URL, key, model IDs, and prices come only from settings.

Never logs the API key or prompts: only task, model, tokens, cost, latency, and attempts.
Reasoning traces are captured for the UI (run_steps.reasoning_excerpt) and never sent back
to the model.
"""

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Any, Protocol, cast

import httpx
import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict

from app.config import Settings, get_settings
from app.llm.budget import BudgetExceededError, RunBudget, UsageStore, estimate_prompt_tokens
from app.llm.models import ModelSpec, Reasoning, Tier, model_spec, ultra_equivalent, usd
from app.llm.router import Route, Task, route
from app.llm.structured import (
    Schema,
    choices_without_other,
    parse_structured,
    response_format,
    with_schema_instruction,
)
from app.llm.tool_calls import (
    ToolCall,
    parse_arguments,
    parse_text_tool_calls,
    raw_text,
    split_reasoning,
)
from app.llm.vectors import fit_dimensions
from app.security.redact import summarize

logger = logging.getLogger("groundcrew.llm")

Message = dict[str, Any]
ToolSchema = dict[str, Any]

# Keys we keep on stored messages for our own bookkeeping. Never sent to the provider.
PRIVATE_KEY_PREFIX = "x_"
# Reasoning keys a provider may put on an assistant message. Never re-sent.
_REASONING_KEYS = frozenset({"reasoning", "reasoning_content"})
# A timed-out request may still be billed, so a timeout is retried once at most.
_MAX_TIMEOUT_RETRIES = 1
# Caps that stop Ultra only; the route's fallback tier still answers.
_ULTRA_CAPS = frozenset({"ultra_call_cap", "incident_ultra_cap"})


class LLMError(RuntimeError):
    """The call failed. The message is redacted and never includes the prompt."""


class LLMConfigError(LLMError):
    pass


class SchemaError(LLMError):
    """The model returned JSON that did not match the schema, twice (first try and repair)."""


class _FallbackEligibleError(Exception):
    """The primary model failed in a way another tier might not (retries exhausted, 5xx,
    prompt too large for its context)."""


class LLMResult(BaseModel):
    """One model call: the answer plus everything the timeline shows about it."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    task: Task | None = None
    tier: Tier | None = None
    model_id: str
    reasoning_mode: Reasoning | None = None
    content: str | None
    tool_calls: list[ToolCall]
    # True when tool calls were parsed from text, not returned natively.
    tool_calls_from_text: bool = False
    # Reasoning trace, for display only. Never part of the conversation history.
    reasoning: str | None = None
    # Set when a schema was requested and the reply validated.
    parsed: Any = None
    finish_reason: str | None
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal = Decimal(0)
    ultra_equiv_usd: Decimal = Decimal(0)
    latency_ms: int
    max_tokens: int | None = None
    attempts: int = 1
    fell_back: bool = False
    escalated: bool = False
    # This call was the schema repair attempt.
    repair: bool = False
    # Short description of what was sent (redacted when stored).
    input_summary: str | None = None

    @property
    def truncated(self) -> bool:
        """The model hit max_tokens: the answer (or a tool call) may be cut off."""
        return self.finish_reason == "length"

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# Phase 1 name.
LLMResponse = LLMResult

OnCall = Callable[[LLMResult], Awaitable[None]]


class ChatModel(Protocol):
    """What agents need. TokenFactory and FakeLLM implement it."""

    async def chat(
        self,
        task: Task,
        messages: Sequence[Message],
        *,
        budget: RunBudget,
        tools: Sequence[ToolSchema] | None = None,
        tool_choice: Any = None,
        schema: Schema | None = None,
        escalate: bool = False,
        max_tokens: int | None = None,
        on_call: OnCall | None = None,
    ) -> LLMResult: ...


def wire_messages(messages: Sequence[Message]) -> list[Message]:
    """Messages as the provider should see them: no private `x_` keys, no reasoning traces."""
    out: list[Message] = []
    for message in messages:
        clean = {k: v for k, v in message.items() if k not in _REASONING_KEYS}
        calls = clean.get("tool_calls")
        if calls:
            clean["tool_calls"] = [
                {k: v for k, v in call.items() if not k.startswith(PRIVATE_KEY_PREFIX)}
                for call in calls
            ]
        out.append(clean)
    return out


def reasoning_kwargs(mode: Reasoning) -> dict[str, Any]:
    """chat_template_kwargs for a reasoning mode.

    Probe (2026-10-07): off gives zero thinking tokens on every tier; low cuts thinking on Nano
    (152 vs 234 completion tokens) and Super (48 vs 144) but not on Ultra (327 vs 218, one
    sample), so routes never send low to Ultra.
    """
    if mode == Reasoning.off:
        return {"enable_thinking": False}
    if mode == Reasoning.low:
        return {"enable_thinking": True, "low_effort": True}
    return {"enable_thinking": True}


def describe_message(message: Message) -> str:
    role = message.get("role", "?")
    if role == "tool":
        return f"tool result: {message.get('content', '')}"
    return f"{role}: {message.get('content') or ''}"


def describe_result(result: LLMResult) -> str:
    """The step's output summary."""
    prefix = "(schema repair) " if result.repair else ""
    if result.tool_calls:
        calls = ", ".join(
            f"{c.name}({c.raw_arguments if c.arguments_error else json.dumps(c.arguments)})"
            for c in result.tool_calls
        )
        source = " (parsed from text)" if result.tool_calls_from_text else ""
        return f"{prefix}tool calls{source}: {calls}"
    return prefix + (result.content or "")


def _redacted_error(exc: BaseException) -> str:
    return summarize(f"{type(exc).__name__}: {exc}", 500) or type(exc).__name__


class TokenFactory:
    """Async Token Factory client. One instance can serve many runs."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        on_call: OnCall | None = None,
        http_client: Any = None,
        rerank_http_client: httpx.AsyncClient | None = None,
        retry_base_seconds: float | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.on_call = on_call
        self.retry_base_seconds = (
            self.settings.llm_retry_base_seconds
            if retry_base_seconds is None
            else retry_base_seconds
        )
        self._rerank_http = rerank_http_client
        # Cleared if the embeddings API rejects the `dimensions` parameter.
        self._send_embed_dimensions = True
        self._connect(http_client)

    def _connect(self, http_client: Any) -> None:
        s = self.settings
        if not s.token_factory_base_url or s.token_factory_api_key is None:
            raise LLMConfigError(
                "TOKEN_FACTORY_BASE_URL and TOKEN_FACTORY_API_KEY must be set. See .env.example."
            )
        self._oai: AsyncOpenAI | None = AsyncOpenAI(
            base_url=s.token_factory_base_url,
            api_key=s.token_factory_api_key.get_secret_value(),
            timeout=s.llm_timeout_seconds,
            max_retries=0,  # we retry ourselves, so each retry is visible and capped
            http_client=http_client,
        )

    # ------------------------------------------------------------------ transport
    # FakeLLM overrides these three; everything else runs the same code in tests.

    def _openai(self) -> AsyncOpenAI:
        if self._oai is None:
            raise LLMConfigError("Token Factory client is not connected.")
        return self._oai

    async def _create(self, kwargs: dict[str, Any]) -> Any:
        return await self._openai().chat.completions.create(**kwargs)

    async def _create_embeddings(self, kwargs: dict[str, Any]) -> Any:
        return await self._openai().embeddings.create(**kwargs)

    async def _post_rerank(self, body: dict[str, Any]) -> dict[str, Any]:
        s = self.settings
        if not s.token_factory_base_url or s.token_factory_api_key is None:
            raise LLMConfigError("TOKEN_FACTORY_BASE_URL and TOKEN_FACTORY_API_KEY must be set.")
        if self._rerank_http is None:
            self._rerank_http = httpx.AsyncClient(timeout=s.llm_timeout_seconds)
        response = await self._rerank_http.post(
            s.token_factory_base_url.rstrip("/") + "/rerank",
            json=body,
            headers={"Authorization": f"Bearer {s.token_factory_api_key.get_secret_value()}"},
        )
        if response.status_code >= 400:
            raise LLMError(f"Token Factory rerank failed: HTTP {response.status_code}")
        data: dict[str, Any] = response.json()
        return data

    async def aclose(self) -> None:
        if self._rerank_http is not None:
            await self._rerank_http.aclose()
        if self._oai is not None:
            await self._oai.close()

    # ----------------------------------------------------------------------- chat

    async def chat(
        self,
        task: Task,
        messages: Sequence[Message],
        *,
        budget: RunBudget,
        tools: Sequence[ToolSchema] | None = None,
        tool_choice: Any = None,
        schema: Schema | None = None,
        escalate: bool = False,
        max_tokens: int | None = None,
        on_call: OnCall | None = None,
    ) -> LLMResult:
        """One task-routed call. `schema` asks for JSON (validated, one repair attempt);
        `escalate` bumps one tier with full reasoning. Raises BudgetExceededError before
        calling when a cap is hit, LLMError when every model failed, SchemaError when the
        JSON is still invalid after the repair."""
        if schema is not None and tools:
            raise ValueError("Use either tools or a schema in one call, not both.")
        if isinstance(schema, type) and (missing := choices_without_other(schema)):
            raise ValueError(
                f"{schema.__name__}: choice fields must allow 'other': {', '.join(missing)}"
            )
        r = route(task, escalate=escalate, settings=self.settings)
        hook = on_call or self.on_call
        sent = list(messages)
        if schema is not None:
            sent = with_schema_instruction(sent, schema)

        tiers = [r.tier] + ([r.fallback] if r.fallback and r.fallback != r.tier else [])
        last_error: BaseException | None = None
        for index, tier in enumerate(tiers):
            try:
                result = await self._checked_call(
                    r, tier, sent, budget, tools, tool_choice, schema, max_tokens
                )
            except BudgetExceededError as exc:
                # The Ultra call caps stop Ultra, not the work: the fallback tier answers.
                if exc.cap not in _ULTRA_CAPS or index + 1 >= len(tiers):
                    raise
                last_error = exc
                logger.info(
                    "llm Ultra cap reached, using the fallback tier",
                    extra={"task": task, "cap": exc.cap, "to_tier": tiers[index + 1]},
                )
                continue
            except _FallbackEligibleError as exc:
                last_error = exc.__cause__ or exc
                if index + 1 < len(tiers):
                    logger.warning(
                        "llm falling back",
                        extra={"task": task, "from_tier": tier, "to_tier": tiers[index + 1]},
                    )
                continue
            result.fell_back = index > 0
            await self._account(result, budget, hook)
            if schema is None:
                return result
            return await self._parse_or_repair(
                r, tier, sent, result, budget, schema, max_tokens, hook
            )

        detail = _redacted_error(last_error) if last_error else "no model available"
        raise LLMError(f"Token Factory call for {task} failed on every model: {detail}")

    async def _parse_or_repair(
        self,
        r: Route,
        tier: Tier,
        sent: list[Message],
        result: LLMResult,
        budget: RunBudget,
        schema: Schema,
        max_tokens: int | None,
        hook: OnCall | None,
    ) -> LLMResult:
        parsed, error = parse_structured(result.content, schema)
        if parsed is not None:
            result.parsed = parsed
            return result
        if result.truncated:
            # Cut off at max_tokens: a "repair" with the same room would be cut off again.
            # Retry once with half again as much room (reasoning included).
            roomier = replace(r, max_tokens=int((result.max_tokens or r.max_tokens or 0) * 1.5))
            try:
                retried = await self._checked_call(
                    roomier, tier, sent, budget, None, None, schema, None
                )
            except _FallbackEligibleError as exc:
                cause = exc.__cause__ or exc
                raise LLMError(
                    f"Retry after a cut-off reply failed: {_redacted_error(cause)}"
                ) from None
            retried.fell_back = result.fell_back
            await self._account(retried, budget, hook)
            parsed, error = parse_structured(retried.content, schema)
            if parsed is not None:
                retried.parsed = parsed
                return retried
            result = retried
        # One repair on the same tier. Budgets are checked again before it.
        repair_messages = [
            *sent,
            {"role": "assistant", "content": result.content or ""},
            {
                "role": "user",
                "content": f"That reply {error}. Return only the corrected JSON object.",
            },
        ]
        try:
            repaired = await self._checked_call(
                r, tier, repair_messages, budget, None, None, schema, max_tokens
            )
        except _FallbackEligibleError as exc:
            cause = exc.__cause__ or exc
            raise LLMError(f"Schema repair call failed: {_redacted_error(cause)}") from None
        repaired.repair = True
        repaired.fell_back = result.fell_back
        await self._account(repaired, budget, hook)
        parsed, error = parse_structured(repaired.content, schema)
        if parsed is None:
            raise SchemaError(f"{r.task} returned invalid JSON twice; last reply {error}.")
        repaired.parsed = parsed
        return repaired

    async def _checked_call(
        self,
        r: Route,
        tier: Tier,
        messages: list[Message],
        budget: RunBudget,
        tools: Sequence[ToolSchema] | None,
        tool_choice: Any,
        schema: Schema | None,
        max_tokens: int | None,
    ) -> LLMResult:
        """Budget checks, then one call (with retries) to `tier`."""
        spec = model_spec(tier, self.settings)  # config errors are not fallback-eligible
        budget.check(tier)
        today = await budget.check_usage()
        prompt_tokens = estimate_prompt_tokens(messages, tools or ())
        # The answer needs room too: a prompt that leaves less than LLM_MIN_COMPLETION_TOKENS
        # of the tier's usable context goes to the fallback tier.
        room = spec.max_context - prompt_tokens
        if room < self.settings.llm_min_completion_tokens:
            raise _FallbackEligibleError(
                f"prompt is about {prompt_tokens} tokens; {tier} context is {spec.max_context}"
            )
        route_cap = r.max_tokens or self.settings.llm_max_tokens
        wanted = min(max_tokens, route_cap) if max_tokens else route_cap
        cap = min(budget.completion_cap(wanted, prompt_tokens, today), room)
        return await self._call(r, spec, messages, tools, tool_choice, schema, cap)

    async def _call(
        self,
        r: Route,
        spec: ModelSpec,
        messages: list[Message],
        tools: Sequence[ToolSchema] | None,
        tool_choice: Any,
        schema: Schema | None,
        cap: int,
    ) -> LLMResult:
        kwargs: dict[str, Any] = {
            "model": spec.model_id,
            "messages": wire_messages(messages),
            "max_tokens": cap,
            "temperature": r.temperature,
            "top_p": r.top_p,
            "extra_body": {"chat_template_kwargs": reasoning_kwargs(r.reasoning)},
        }
        if tools:
            kwargs["tools"] = list(tools)
            kwargs["tool_choice"] = tool_choice or "auto"
        if schema is not None:
            kwargs["response_format"] = response_format(schema)
        if spec.tier == Tier.ultra:
            # Ultra with full reasoning can run longer than the default timeout; a timeout is
            # retried, and both attempts would be billed.
            kwargs["timeout"] = max(
                self.settings.llm_timeout_seconds, self.settings.llm_timeout_seconds_ultra
            )

        start = time.perf_counter()
        completion, attempts = await self._create_with_retries(kwargs, spec.model_id)
        latency_ms = round((time.perf_counter() - start) * 1000)

        result = self._parse(completion, spec, r, tools, latency_ms)
        result.max_tokens = cap
        result.attempts = attempts
        result.input_summary = (
            f"{len(messages)} messages; last {describe_message(messages[-1])}" if messages else None
        )
        logger.info(
            "llm call",
            extra={
                "task": r.task,
                "tier": spec.tier,
                "model_id": spec.model_id,
                "reasoning_mode": r.reasoning,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "cost_usd": str(result.cost_usd),
                "latency_ms": latency_ms,
                "attempts": attempts,
                "tool_calls": len(result.tool_calls),
                "finish_reason": result.finish_reason,
                "escalated": r.escalated,
            },
        )
        return result

    async def _create_with_retries(self, kwargs: dict[str, Any], model_id: str) -> tuple[Any, int]:
        attempts = 0
        timeouts = 0
        error: openai.OpenAIError
        while True:
            attempts += 1
            try:
                return await self._create(kwargs), attempts
            except openai.APITimeoutError as exc:  # before APIConnectionError: it subclasses it
                timeouts += 1
                if timeouts > _MAX_TIMEOUT_RETRIES or attempts >= self.settings.llm_max_attempts:
                    raise _FallbackEligibleError(str(exc)) from exc
                error = exc
            except (
                openai.RateLimitError,
                openai.InternalServerError,
                openai.APIConnectionError,
            ) as exc:
                if attempts >= self.settings.llm_max_attempts:
                    raise _FallbackEligibleError(str(exc)) from exc
                error = exc
            except openai.APIStatusError as exc:
                # 4xx other than 429: a bad request, key, or model. Retrying will not help.
                raise LLMError(
                    f"Token Factory call to {model_id} failed: {_redacted_error(exc)}"
                ) from None
            except openai.OpenAIError as exc:
                raise LLMError(
                    f"Token Factory call to {model_id} failed: {_redacted_error(exc)}"
                ) from None
            delay = self.retry_base_seconds * (2 ** (attempts - 1)) + random.uniform(  # noqa: S311
                0, self.retry_base_seconds
            )
            logger.warning(
                "llm call retrying",
                extra={"model_id": model_id, "error": type(error).__name__, "attempt": attempts},
            )
            await asyncio.sleep(delay)

    def _parse(
        self,
        completion: Any,
        spec: ModelSpec,
        r: Route,
        tools: Sequence[ToolSchema] | None,
        latency_ms: int,
    ) -> LLMResult:
        usage = completion.usage
        prompt_tokens = int(usage.prompt_tokens) if usage else 0
        completion_tokens = int(usage.completion_tokens) if usage else 0
        if not completion.choices:
            raise LLMError(f"Token Factory returned no choices for {spec.model_id}.")
        choice = completion.choices[0]
        message = choice.message

        # Probe (2026-10-07): Nano returns the trace in `reasoning`, Super and Ultra in
        # `reasoning_content`; no <think> tags were seen. Inline tags are still handled.
        extra = message.model_extra or {}
        trace = extra.get("reasoning_content") or extra.get("reasoning")
        content = message.content
        if content:
            content, inline = split_reasoning(content)
            trace = trace or inline

        tool_calls = []
        for call in message.tool_calls or []:
            function = cast(Any, call).function
            arguments, error = parse_arguments(function.arguments)
            tool_calls.append(
                ToolCall(
                    id=call.id,
                    name=function.name,
                    arguments=arguments,
                    arguments_error=error,
                    raw_arguments=raw_text(function.arguments) if error else None,
                )
            )

        from_text = False
        if not tool_calls and content and tools:
            names = [t["function"]["name"] for t in tools]
            tool_calls = parse_text_tool_calls(content, names)
            from_text = bool(tool_calls)
            if from_text:
                content = None

        # finish_reason="length" is not an error here: the tokens were spent and must be
        # recorded. The caller decides what a truncated response means.
        return LLMResult(
            task=r.task,
            tier=spec.tier,
            model_id=spec.model_id,
            reasoning_mode=r.reasoning,
            content=content or None,
            tool_calls=tool_calls,
            tool_calls_from_text=from_text,
            reasoning=str(trace) if trace else None,
            finish_reason=choice.finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=spec.cost(prompt_tokens, completion_tokens),
            ultra_equiv_usd=ultra_equivalent(prompt_tokens, completion_tokens, self.settings),
            latency_ms=latency_ms,
            escalated=r.escalated,
        )

    async def _account(self, result: LLMResult, budget: RunBudget, hook: OnCall | None) -> None:
        if result.tier is None:
            raise LLMError("A chat result without a tier cannot be counted.")
        budget.record(result.tier, result.cost_usd, result.total_tokens, result.ultra_equiv_usd)
        await budget.usage.add(result.cost_usd, result.total_tokens)
        if hook is not None:
            await hook(result)

    # -------------------------------------------------------------- embed / rerank

    async def embed(
        self, texts: list[str], *, budget: RunBudget | None = None, usage: UsageStore | None = None
    ) -> list[list[float]]:
        """L2-normalised embeddings of EMBED_DIM values with MODEL_EMBED (Qwen3-Embedding-8B,
        the only embedding model on Token Factory). Refuses without PRICE_EMBED_PER_M.

        Asks for `dimensions=EMBED_DIM`. If the API rejects that parameter, it is dropped for
        this client and the call retried once. Longer vectors are cut to their first EMBED_DIM
        values and re-normalised (valid for Matryoshka models), so incidents.embedding stays
        at 1024 with no migration.
        """
        s = self.settings
        if not s.model_embed:
            raise LLMConfigError("MODEL_EMBED is not set.")
        if s.price_embed_per_m is None:
            raise LLMConfigError("PRICE_EMBED_PER_M is not set; refusing an uncounted call.")
        store = budget.usage if budget is not None else usage
        if store is None:
            raise LLMConfigError("embed() needs a budget or a usage store to count spend.")
        await _check_store(store, budget, s)
        request: dict[str, Any] = {
            "model": s.model_embed,
            "input": texts,
            "encoding_format": "float",
        }
        if self._send_embed_dimensions:
            request["dimensions"] = s.embed_dim
        try:
            response = await self._create_embeddings(request)
        except openai.APIStatusError as exc:
            if "dimensions" not in request or exc.status_code not in (400, 422):
                raise LLMError(f"Token Factory embeddings failed: {_redacted_error(exc)}") from None
            logger.info("embeddings: dimensions parameter rejected; truncating instead")
            self._send_embed_dimensions = False
            request.pop("dimensions")
            try:
                response = await self._create_embeddings(request)
            except openai.OpenAIError as retry_exc:
                raise LLMError(
                    f"Token Factory embeddings failed: {_redacted_error(retry_exc)}"
                ) from None
        except openai.OpenAIError as exc:
            raise LLMError(f"Token Factory embeddings failed: {_redacted_error(exc)}") from None
        raw = [list(d.embedding) for d in response.data]
        tokens = int(getattr(response.usage, "prompt_tokens", 0) or 0) if response.usage else 0
        cost = usd(tokens, s.price_embed_per_m)
        if budget is not None:
            budget.record_spend(cost, tokens)
        await store.add(cost, tokens)
        try:
            return [fit_dimensions(vector, s.embed_dim) for vector in raw]
        except ValueError as exc:
            raise LLMError(
                f"{s.model_embed}: {exc} (EMBED_DIM={s.embed_dim}). "
                "Use another model or a migration."
            ) from None

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int = 8,
        *,
        budget: RunBudget | None = None,
        usage: UsageStore | None = None,
    ) -> list[tuple[int, float]]:
        """(document index, score), best first. Without MODEL_RERANK: input order, score 0.

        No rerank model is available on Token Factory yet (probe, 2026-10-07), so evidence
        ranking uses embedding similarity by default (app/llm/evidence.py). This stays for when
        one appears.
        TODO(nebius): the rerank response's `usage` fields are not documented in detail; cost
        uses usage.total_tokens / prompt_tokens when present, else about 4 characters per token.
        https://docs.tokenfactory.nebius.com/llms.txt
        """
        s = self.settings
        if not s.model_rerank or not documents:
            return [(i, 0.0) for i in range(min(top_k, len(documents)))]
        if s.price_rerank_per_m is None:
            raise LLMConfigError("PRICE_RERANK_PER_M is not set; refusing an uncounted call.")
        store = budget.usage if budget is not None else usage
        if store is None:
            raise LLMConfigError("rerank() needs a budget or a usage store to count spend.")
        await _check_store(store, budget, s)
        data = await self._post_rerank(
            {"model": s.model_rerank, "query": query, "documents": documents}
        )
        used = data.get("usage") or {}
        tokens = int(
            used.get("total_tokens")
            or used.get("prompt_tokens")
            or (len(query) + sum(len(d) for d in documents)) // 4
        )
        cost = usd(tokens, s.price_rerank_per_m)
        if budget is not None:
            budget.record_spend(cost, tokens)
        await store.add(cost, tokens)
        scored = [(int(x["index"]), float(x["relevance_score"])) for x in data.get("results", [])]
        return sorted(scored, key=lambda pair: -pair[1])[:top_k]


async def _check_store(store: UsageStore, budget: RunBudget | None, settings: Settings) -> None:
    if budget is not None:
        if budget.spent_usd >= Decimal(str(settings.llm_run_usd_cap)):
            raise BudgetExceededError("run_usd_cap", f"run spent ${budget.spent_usd}")
        await budget.check_usage()
        return
    await RunBudget(usage=store, settings=settings).check_usage()


# Phase 1 name.
TokenFactoryClient = TokenFactory
