"""Two-dial routing: task -> tier + reasoning mode, escalation, DEV_MODE_NO_ULTRA."""

from typing import Any

import pytest

from app.config import Settings
from app.llm.models import Reasoning, Tier, next_tier
from app.llm.router import (
    ROUTES,
    ModelNotConfiguredError,
    Task,
    TaskType,
    lightning_enabled,
    model_id,
    route,
)


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "model_nano": "m/nano",
        "model_super": "m/super",
        "model_ultra": "m/ultra",
        **overrides,
    }
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("task", "tier", "reasoning"),
    [
        (Task.summarize_graph, Tier.nano, Reasoning.off),
        (Task.detect_app_spec, Tier.nano, Reasoning.low),
        (Task.write_dockerfile, Tier.super, Reasoning.low),
        (Task.fix_build, Tier.super, Reasoning.on),
        (Task.plan_deploy, Tier.super, Reasoning.low),
        (Task.root_cause, Tier.ultra, Reasoning.on),
        (Task.propose_fix, Tier.ultra, Reasoning.on),
        (Task.summarize, Tier.nano, Reasoning.off),  # Echo Ops keeps its Phase 1 route
        (Task.plan, Tier.super, Reasoning.off),
    ],
)
def test_routes(task: Task, tier: Tier, reasoning: Reasoning) -> None:
    r = route(task, settings=_settings())
    assert (r.tier, r.reasoning, r.escalated) == (tier, reasoning, False)


def test_every_task_has_a_route() -> None:
    assert set(ROUTES) == set(Task)
    assert TaskType is Task


LIGHTNING_PRICED = {"price_lightning_input_per_m": 0.05, "price_lightning_output_per_m": 0.2}


def test_ultra_and_lightning_never_run_with_low_reasoning() -> None:
    # Probes: low saved little or cost more on these tiers. Holds for every task, escalated or
    # not, with and without DEV_MODE_NO_ULTRA and Lightning routing.
    for overrides in ({}, {"dev_mode_no_ultra": True}, LIGHTNING_PRICED):
        settings = _settings(**overrides)
        for task in Task:
            for escalate in (False, True):
                r = route(task, escalate=escalate, settings=settings)
                low_on_big = r.tier in (Tier.ultra, Tier.lightning) and r.reasoning == Reasoning.low
                assert not low_on_big, (task, overrides)


def test_no_task_routes_to_lightning_until_priced() -> None:
    for task in Task:
        for escalate in (False, True):
            r = route(task, escalate=escalate, settings=_settings())
            assert Tier.lightning not in (r.tier, r.fallback)
    half = _settings(price_lightning_input_per_m=0.05)  # both prices are needed
    assert route(Task.tool_step, settings=half).tier == Tier.super
    assert not lightning_enabled(half)


@pytest.mark.parametrize(
    ("task", "fallback"),
    [
        (Task.tool_step, Tier.super),
        (Task.investigate_step, Tier.super),
        (Task.verify_recovery, Tier.nano),
    ],
)
def test_lightning_routes_once_priced(task: Task, fallback: Tier) -> None:
    r = route(task, settings=_settings(**LIGHTNING_PRICED))
    assert (r.tier, r.reasoning, r.fallback) == (Tier.lightning, Reasoning.off, fallback)
    up = route(task, escalate=True, settings=_settings(**LIGHTNING_PRICED))
    assert (up.tier, up.reasoning) == (Tier.super, Reasoning.on)  # Lightning escalates to Super


def test_nano_keeps_its_tasks_when_lightning_is_on() -> None:
    settings = _settings(**LIGHTNING_PRICED)
    for task in (
        Task.classify_signal,
        Task.summarize_graph,
        Task.compress_evidence,
        Task.detect_app_spec,
    ):
        assert route(task, settings=settings).tier == Tier.nano


def test_lightning_escalates_to_super() -> None:
    assert next_tier(Tier.lightning) == Tier.super
    assert next_tier(Tier.nano) == Tier.super


def test_escalate_bumps_one_tier_with_full_reasoning() -> None:
    base = route(Task.fix_build, settings=_settings())
    up = route(Task.fix_build, escalate=True, settings=_settings())
    assert (up.tier, up.reasoning, up.escalated) == (Tier.ultra, Reasoning.on, True)
    assert up.fallback == Tier.super  # the tier it came from
    assert up.max_tokens == int((base.max_tokens or 0) * 1.5)


def test_escalating_ultra_stays_ultra() -> None:
    assert next_tier(Tier.ultra) == Tier.ultra
    r = route(Task.root_cause, escalate=True, settings=_settings())
    # The fallback equals the tier, so the client has nothing to fall back to.
    assert (r.tier, r.fallback, r.reasoning) == (Tier.ultra, Tier.ultra, Reasoning.on)


def test_escalating_a_generic_task_uses_llm_max_tokens() -> None:
    r = route(Task.summarize, escalate=True, settings=_settings(llm_max_tokens=1000))
    assert (r.tier, r.max_tokens) == (Tier.super, 1500)


def test_dev_mode_no_ultra() -> None:
    s = _settings(dev_mode_no_ultra=True)
    assert route(Task.root_cause, settings=s).tier == Tier.super
    assert route(Task.root_cause, settings=s).fallback is None  # Super fallback == tier
    up = route(Task.fix_build, escalate=True, settings=s)
    assert (up.tier, up.reasoning, up.fallback) == (Tier.super, Reasoning.on, None)


def test_model_ids_come_from_settings() -> None:
    assert model_id(Task.plan_deploy, _settings()) == "m/super"
    with pytest.raises(ModelNotConfiguredError, match="MODEL_NANO"):
        model_id(Task.detect, _settings(model_nano=None))
