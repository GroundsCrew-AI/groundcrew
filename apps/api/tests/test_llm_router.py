"""Two-dial routing: task -> tier + reasoning mode, escalation, DEV_MODE_NO_ULTRA."""

from typing import Any

import pytest

from app.config import Settings
from app.llm.models import Reasoning, Tier, next_tier
from app.llm.router import ROUTES, ModelNotConfiguredError, Task, TaskType, model_id, route


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
