"""TokenFactory.chat behaviour, driven by the scripted FakeLLM: schema output and repair,
fallback, escalation, retries, reasoning capture (never re-sent), cost, the on_call hook."""

from decimal import Decimal
from typing import Any, Literal

import pytest
from pydantic import BaseModel

from app.config import Settings
from app.llm.budget import BudgetExceededError, MemoryUsageStore, RunBudget
from app.llm.client import LLMError, LLMResult, SchemaError
from app.llm.models import PriceMissingError, Reasoning, Tier
from app.llm.router import Task
from tests.fakes import FakeLLM, reply, status_error, timeout_error

pytestmark = pytest.mark.anyio

MESSAGES = [
    {"role": "system", "content": "You are Scout."},
    {"role": "user", "content": "Describe this repo."},
]


class Spec(BaseModel):
    language: str
    port: int
    needs_database: bool = False


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "model_nano": "m/nano",
        "model_super": "m/super",
        "model_ultra": "m/ultra",
        **overrides,
    }
    return Settings(_env_file=None, **values)


def _budget(store: MemoryUsageStore | None = None, **caps: Any) -> RunBudget:
    return RunBudget(usage=store or MemoryUsageStore(), settings=_settings(**caps))


async def test_schema_output_is_parsed_and_sent_both_ways() -> None:
    llm = FakeLLM(reply('{"language": "python", "port": 8000}'), settings=_settings())
    result = await llm.chat(Task.detect_app_spec, MESSAGES, budget=_budget(), schema=Spec)
    assert result.parsed == Spec(language="python", port=8000)
    [sent] = llm.calls
    fmt = sent["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    schema = fmt["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["language", "port", "needs_database"]
    # The schema is also in the prompt, merged into the one system message.
    system = [m for m in sent["messages"] if m["role"] == "system"]
    assert len(system) == 1
    assert system[0]["content"].startswith("You are Scout.") and '"port"' in system[0]["content"]


async def test_invalid_json_is_repaired_once() -> None:
    store = MemoryUsageStore()
    steps: list[LLMResult] = []

    async def hook(result: LLMResult) -> None:
        steps.append(result)

    llm = FakeLLM(
        reply('{"language": "python", "port": '),  # cut-off JSON
        reply('```json\n{"language": "python", "port": 8000}\n```'),
        settings=_settings(),
    )
    budget = _budget(store)
    result = await llm.chat(
        Task.detect_app_spec, MESSAGES, budget=budget, schema=Spec, on_call=hook
    )
    assert result.parsed == Spec(language="python", port=8000)
    assert result.repair and [s.repair for s in steps] == [False, True]
    repair_prompt = llm.calls[1]["messages"][-1]["content"]
    assert "not valid JSON" in repair_prompt and "corrected JSON" in repair_prompt
    # Both calls are counted.
    assert store.tokens_today == 240 and budget.tokens == 240


async def test_schema_mismatch_names_the_field_in_the_repair() -> None:
    llm = FakeLLM(
        reply('{"language": "python", "port": "eight thousand"}'),
        reply('{"language": "python", "port": 8000}'),
        settings=_settings(),
    )
    await llm.chat(Task.detect_app_spec, MESSAGES, budget=_budget(), schema=Spec)
    assert "port" in llm.calls[1]["messages"][-1]["content"]


async def test_still_invalid_after_repair_raises() -> None:
    llm = FakeLLM(reply("not json"), reply("still not json"), settings=_settings())
    with pytest.raises(SchemaError, match="invalid JSON twice"):
        await llm.chat(Task.detect_app_spec, MESSAGES, budget=_budget(), schema=Spec)
    assert len(llm.calls) == 2  # exactly one repair


async def test_repair_respects_the_budget() -> None:
    llm = FakeLLM(reply("not json"), reply('{"language": "go", "port": 1}'), settings=_settings())
    budget = _budget(max_tokens_per_run=700, llm_min_completion_tokens=500)
    with pytest.raises(BudgetExceededError, match="run_token_cap"):
        await llm.chat(Task.detect_app_spec, MESSAGES, budget=budget, schema=Spec)
    assert len(llm.calls) == 1  # the repair was never sent


async def test_reasoning_is_captured_but_never_resent() -> None:
    llm = FakeLLM(
        reply("first answer", reasoning="private chain of thought"),
        reply("second answer"),
        settings=_settings(),
    )
    first = await llm.chat(Task.root_cause, MESSAGES, budget=_budget())
    assert first.reasoning == "private chain of thought"
    assert first.reasoning_mode == Reasoning.on
    # Even if a caller stores the trace on the message, the client strips it before sending.
    history: list[dict[str, Any]] = [
        *MESSAGES,
        {"role": "assistant", "content": first.content, "reasoning_content": first.reasoning},
        {"role": "user", "content": "And then?"},
    ]
    await llm.chat(Task.root_cause, history, budget=_budget())
    sent = str(llm.calls[1]["messages"])
    assert "private chain of thought" not in sent and "first answer" in sent


async def test_inline_think_tags_are_captured() -> None:
    llm = FakeLLM(reply("answer", reasoning="hmm", reasoning_field="inline"), settings=_settings())
    result = await llm.chat(Task.root_cause, MESSAGES, budget=_budget())
    assert (result.content, result.reasoning) == ("answer", "hmm")


async def test_cost_and_ultra_equivalent() -> None:
    llm = FakeLLM(
        reply("ok", prompt_tokens=1_000_000, completion_tokens=500_000), settings=_settings()
    )
    store = MemoryUsageStore()
    budget = _budget(store, llm_run_usd_cap=10.0, max_tokens_per_run=10_000_000)
    result = await llm.chat(Task.summarize_graph, MESSAGES, budget=budget)
    # Nano 0.06 / 0.24 per million; Ultra 1.00 / 3.00.
    assert result.cost_usd == Decimal("0.180000")
    assert result.ultra_equiv_usd == Decimal("2.500000")
    assert budget.spent_usd == Decimal("0.18") and store.usd_today == Decimal("0.18")
    assert budget.savings_pct == pytest.approx(92.8)


async def test_fallback_on_5xx_after_retries() -> None:
    llm = FakeLLM(
        status_error(503),
        status_error(503),
        status_error(503),  # Nano fails LLM_MAX_ATTEMPTS times
        reply('{"language": "node", "port": 3000}'),  # Super answers
        settings=_settings(),
    )
    result = await llm.chat(Task.detect_app_spec, MESSAGES, budget=_budget(), schema=Spec)
    assert result.fell_back and result.tier == Tier.super
    assert llm.models == ["m/nano"] * 3 + ["m/super"]


async def test_no_fallback_on_4xx() -> None:
    llm = FakeLLM(status_error(401), reply("never sent"), settings=_settings())
    with pytest.raises(LLMError):
        await llm.chat(Task.detect_app_spec, MESSAGES, budget=_budget(), schema=Spec)
    assert llm.models == ["m/nano"]


async def test_timeout_is_retried_once_only() -> None:
    llm = FakeLLM(timeout_error(), timeout_error(), reply("never sent"), settings=_settings())
    with pytest.raises(LLMError, match="every model"):
        await llm.chat(Task.summarize_graph, MESSAGES, budget=_budget())
    assert len(llm.calls) == 2


async def test_429_is_retried() -> None:
    llm = FakeLLM(status_error(429), reply("ok"), settings=_settings())
    result = await llm.chat(Task.summarize_graph, MESSAGES, budget=_budget())
    assert result.content == "ok" and result.attempts == 2


async def test_escalation_goes_up_a_tier_and_falls_back_down() -> None:
    llm = FakeLLM(
        status_error(500),
        status_error(500),
        status_error(500),
        reply("fixed"),
        settings=_settings(),
    )
    result = await llm.chat(Task.fix_build, MESSAGES, budget=_budget(), escalate=True)
    assert llm.models == ["m/ultra"] * 3 + ["m/super"]
    assert result.escalated and result.fell_back and result.tier == Tier.super
    assert llm.reasoning_kwargs[0] == {"enable_thinking": True}
    assert llm.max_tokens[0] == 9000  # 6000 * 1.5


async def test_budget_is_checked_before_the_call() -> None:
    llm = FakeLLM(reply("never sent"), settings=_settings())
    store = MemoryUsageStore(usd_before_today=Decimal("25"))
    with pytest.raises(BudgetExceededError, match="total_usd_cap"):
        await llm.chat(Task.summarize_graph, MESSAGES, budget=_budget(store))
    assert llm.calls == []


async def test_ultra_cap_blocks_ultra_but_not_its_fallback() -> None:
    llm = FakeLLM(reply("never sent"), settings=_settings())
    budget = RunBudget(usage=MemoryUsageStore(), settings=_settings(), ultra_calls=3)
    with pytest.raises(BudgetExceededError, match="ultra_call_cap"):
        await llm.chat(Task.root_cause, MESSAGES, budget=budget)
    assert llm.calls == []


async def test_model_without_price_is_refused() -> None:
    llm = FakeLLM(reply("never sent"), settings=_settings(price_nano_input_per_m=None))
    with pytest.raises(PriceMissingError, match="PRICE_NANO_INPUT_PER_M"):
        await llm.chat(Task.summarize_graph, MESSAGES, budget=_budget())
    assert llm.calls == []


async def test_route_cap_and_caller_cap() -> None:
    llm = FakeLLM(reply("a"), reply("b"), settings=_settings())
    await llm.chat(Task.classify_signal, MESSAGES, budget=_budget())
    await llm.chat(Task.fix_build, MESSAGES, budget=_budget(), max_tokens=2000)
    assert llm.max_tokens == [300, 2000]


async def test_prompt_too_large_for_nano_falls_back_to_super() -> None:
    big = [{"role": "user", "content": "x" * 140_000}]  # about 35k tokens > Nano's 32k
    llm = FakeLLM(reply('{"language": "go", "port": 8080}'), settings=_settings())
    result = await llm.chat(Task.detect_app_spec, big, budget=_budget(), schema=Spec)
    assert result.tier == Tier.super and result.fell_back
    assert llm.models == ["m/super"]


async def test_tools_and_schema_together_are_rejected() -> None:
    llm = FakeLLM(settings=_settings())
    with pytest.raises(ValueError, match="either tools or a schema"):
        await llm.chat(
            Task.tool_step, MESSAGES, budget=_budget(), tools=[{"function": {}}], schema=Spec
        )


async def test_embed_asks_for_embed_dim_and_normalizes() -> None:
    settings = _settings(price_embed_per_m=0.01, embed_dim=2)
    store = MemoryUsageStore()
    llm = FakeLLM(settings=settings, embeddings=[[[3.0, 4.0]]])
    assert await llm.embed(["oom"], usage=store) == [[0.6, 0.8]]
    [request] = llm.calls
    assert request["model"] == "Qwen/Qwen3-Embedding-8B"  # the default
    assert request["dimensions"] == 2
    assert store.tokens_today == 8


async def test_embed_truncates_when_dimensions_are_ignored() -> None:
    # Qwen3-Embedding-8B returns 4096 values; a Matryoshka prefix is a valid embedding.
    settings = _settings(price_embed_per_m=0.01, embed_dim=2)
    llm = FakeLLM(settings=settings, embeddings=[[[3.0, 4.0, 99.0, 99.0]]])
    assert await llm.embed(["oom"], usage=MemoryUsageStore()) == [[0.6, 0.8]]


async def test_embed_drops_rejected_dimensions_parameter_once() -> None:
    settings = _settings(price_embed_per_m=0.01, embed_dim=2)
    llm = FakeLLM(
        settings=settings,
        embeddings=[status_error(400, "unknown field dimensions"), [[0.0, 2.0, 5.0]], [[1.0, 0.0]]],
    )
    assert await llm.embed(["a"], usage=MemoryUsageStore()) == [[0.0, 1.0]]
    assert await llm.embed(["b"], usage=MemoryUsageStore()) == [[1.0, 0.0]]
    assert ["dimensions" in c for c in llm.calls] == [True, False, False]


async def test_embed_too_short_is_an_error() -> None:
    settings = _settings(price_embed_per_m=0.01, embed_dim=4)
    llm = FakeLLM(settings=settings, embeddings=[[[0.1, 0.2]]])
    with pytest.raises(LLMError, match="EMBED_DIM"):
        await llm.embed(["oom"], usage=MemoryUsageStore())


async def test_lightning_is_refused_without_a_price() -> None:
    from app.llm.models import model_spec

    with pytest.raises(PriceMissingError, match="PRICE_LIGHTNING_INPUT_PER_M"):
        model_spec(Tier.lightning, _settings())  # the model ID has a default; the price does not
    spec = model_spec(
        Tier.lightning,
        _settings(
            model_lightning="nvidia/Nemotron-3_5-Lightning",
            price_lightning_input_per_m=0.05,
            price_lightning_output_per_m=0.2,
        ),
    )
    assert (spec.model_id, spec.max_context) == ("nvidia/Nemotron-3_5-Lightning", 64_000)


async def test_schema_values_are_normalized_before_validation() -> None:
    class Lang(BaseModel):
        language: Literal["javascript", "typescript", "python", "other"]
        port: int

    llm = FakeLLM(reply('{"language": "TypeScript", "port": 3000}'), settings=_settings())
    result = await llm.chat(Task.detect_app_spec, MESSAGES, budget=_budget(), schema=Lang)
    assert result.parsed == Lang(language="typescript", port=3000)
    assert len(llm.calls) == 1  # no repair needed


async def test_embed_and_rerank_refuse_without_price() -> None:
    llm = FakeLLM(settings=_settings(model_rerank="r/model"))
    with pytest.raises(LLMError, match="PRICE_EMBED_PER_M"):
        await llm.embed(["x"], usage=MemoryUsageStore())
    with pytest.raises(LLMError, match="PRICE_RERANK_PER_M"):
        await llm.rerank("q", ["a"], usage=MemoryUsageStore())


async def test_rerank_without_model_keeps_order() -> None:
    llm = FakeLLM(settings=_settings())
    assert await llm.rerank("q", ["a", "b", "c"], top_k=2) == [(0, 0.0), (1, 0.0)]


async def test_nano_prompts_over_its_usable_context_go_to_super() -> None:
    # About 8.5k tokens: within what Nano accepts, past what it reads reliably (8000).
    big = [{"role": "user", "content": "x" * 34_000}]
    llm = FakeLLM(reply('{"language": "go", "port": 8080}'), settings=_settings())
    result = await llm.chat(Task.detect_app_spec, big, budget=_budget(), schema=Spec)
    assert result.tier == Tier.super and result.fell_back


async def test_lightning_route_is_used_once_priced() -> None:
    priced = _settings(price_lightning_input_per_m=0.05, price_lightning_output_per_m=0.2)
    llm = FakeLLM(reply("next"), reply("next"), settings=priced)
    result = await llm.chat(Task.tool_step, MESSAGES, budget=_budget())
    assert result.tier == Tier.lightning and llm.models == ["nvidia/Nemotron-3_5-Lightning"]
    assert llm.reasoning_kwargs == [{"enable_thinking": False}]
    unpriced = FakeLLM(reply("next"), settings=_settings())
    assert (await unpriced.chat(Task.tool_step, MESSAGES, budget=_budget())).tier == Tier.super


async def test_schema_without_other_is_refused() -> None:
    class Closed(BaseModel):
        language: Literal["python", "go"]

    llm = FakeLLM(reply('{"language": "go"}'), settings=_settings())
    with pytest.raises(ValueError, match="must allow 'other': language"):
        await llm.chat(Task.detect_app_spec, MESSAGES, budget=_budget(), schema=Closed)
    assert llm.calls == []


async def test_schema_prompt_allows_other_and_null() -> None:
    llm = FakeLLM(reply('{"language": "go", "port": 1}'), settings=_settings())
    await llm.chat(Task.detect_app_spec, MESSAGES, budget=_budget(), schema=Spec)
    system = llm.calls[0]["messages"][0]["content"]
    assert 'use "other"' in system and "use null" in system
