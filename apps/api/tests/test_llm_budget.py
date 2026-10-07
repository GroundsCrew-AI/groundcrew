"""Spend guards: per-run USD/token/Ultra caps, the llm_usage store, daily and total caps."""

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.models import LlmUsage, Run, RunStep, Workspace
from app.db.session import SessionFactory
from app.llm.budget import (
    BudgetExceededError,
    MemoryUsageStore,
    PgUsageStore,
    RunBudget,
    Usage,
)
from app.llm.models import Tier

pytestmark = pytest.mark.anyio


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def _budget(store: Any = None, **kwargs: Any) -> RunBudget:
    caps = {k: v for k, v in kwargs.items() if k not in RunBudget.__dataclass_fields__}
    state = {k: v for k, v in kwargs.items() if k in RunBudget.__dataclass_fields__}
    return RunBudget(usage=store or MemoryUsageStore(), settings=_settings(**caps), **state)


def _cap(exc_info: pytest.ExceptionInfo[BudgetExceededError]) -> str:
    return exc_info.value.cap


def test_run_usd_cap() -> None:
    _budget(spent_usd=Decimal("0.39")).check(Tier.nano)
    with pytest.raises(BudgetExceededError) as exc_info:
        _budget(spent_usd=Decimal("0.40")).check(Tier.nano)
    assert _cap(exc_info) == "run_usd_cap"
    assert "LLM_RUN_USD_CAP=0.4" in str(exc_info.value)


def test_run_token_cap() -> None:
    with pytest.raises(BudgetExceededError) as exc_info:
        _budget(tokens=1000, max_tokens_per_run=1000).check(Tier.nano)
    assert _cap(exc_info) == "run_token_cap"


def test_ultra_caps() -> None:
    _budget(ultra_calls=3).check(Tier.super)  # only Ultra calls count
    with pytest.raises(BudgetExceededError) as exc_info:
        _budget(ultra_calls=3).check(Tier.ultra)
    assert _cap(exc_info) == "ultra_call_cap"
    with pytest.raises(BudgetExceededError) as exc_info:
        _budget(incident_ultra_calls=2, max_ultra_calls_per_incident=2).check(Tier.ultra)
    assert _cap(exc_info) == "incident_ultra_cap"


async def test_daily_and_total_caps() -> None:
    store = MemoryUsageStore(usd_today=Decimal("1.99"), tokens_today=10)
    assert (await _budget(store).check_usage()).tokens == 10
    store.usd_today = Decimal("2.00")
    with pytest.raises(BudgetExceededError) as exc_info:
        await _budget(store).check_usage()
    assert _cap(exc_info) == "daily_usd_cap"
    # The daily cap is configurable; the total cap is the hard stop.
    await _budget(store, llm_daily_usd_cap=5.0).check_usage()
    store.usd_before_today = Decimal("23.00")
    with pytest.raises(BudgetExceededError) as exc_info:
        await _budget(store, llm_daily_usd_cap=5.0).check_usage()
    assert _cap(exc_info) == "total_usd_cap"


async def test_daily_token_budget() -> None:
    store = MemoryUsageStore(tokens_today=500)
    with pytest.raises(BudgetExceededError) as exc_info:
        await _budget(store, daily_token_budget=500).check_usage()
    assert _cap(exc_info) == "daily_token_cap"


def test_completion_cap_cuts_to_what_is_left() -> None:
    small = {"llm_min_completion_tokens": 50}
    today = Usage(Decimal(0), 0)
    assert (
        _budget(tokens=900, max_tokens_per_run=1000, **small).completion_cap(500, 0, today) == 100
    )
    assert (
        _budget(tokens=900, max_tokens_per_run=1000, **small).completion_cap(500, 30, today) == 70
    )
    assert _budget(**small).completion_cap(64, 0, today) == 64
    with pytest.raises(BudgetExceededError) as exc_info:
        _budget(tokens=990, max_tokens_per_run=1000, **small).completion_cap(500, 0, today)
    assert _cap(exc_info) == "run_token_cap"
    with pytest.raises(BudgetExceededError) as exc_info:
        _budget(daily_token_budget=1000, **small).completion_cap(500, 0, Usage(Decimal(0), 980))
    assert _cap(exc_info) == "daily_token_cap"
    # A route that only wants 300 tokens is not blocked by a 512-token floor.
    assert _budget(tokens=0).completion_cap(300, 0, today) == 300


def test_record_and_savings() -> None:
    budget = _budget()
    budget.record(Tier.nano, Decimal("0.01"), 1000, Decimal("0.10"))
    budget.record(Tier.ultra, Decimal("0.05"), 500, Decimal("0.05"))
    assert (budget.spent_usd, budget.tokens, budget.ultra_calls) == (Decimal("0.06"), 1500, 1)
    assert budget.savings_pct == pytest.approx(60.0)


async def test_pg_usage_store_accumulates_per_day(session_factory: SessionFactory) -> None:
    store = PgUsageStore(session_factory)
    assert await store.today() == Usage(Decimal(0), 0)
    await store.add(Decimal("0.010000"), 100)
    await store.add(Decimal("0.005000"), 50)
    assert await store.today() == Usage(Decimal("0.015000"), 150)
    assert await store.total_usd() == Decimal("0.015000")
    async with session_factory() as session:
        rows = (await session.scalars(select(LlmUsage))).all()
    assert [(r.tokens, r.calls) for r in rows] == [(150, 2)]  # one row per UTC day


async def test_budget_loads_from_run(session_factory: SessionFactory, workspace: Workspace) -> None:
    async with session_factory() as session:
        run = Run(
            workspace_id=workspace.id,
            agent="echo_ops",
            goal="g",
            prompt_tokens=300,
            completion_tokens=50,
            cost_usd=Decimal("0.120000"),
            ultra_equiv_usd=Decimal("0.300000"),
        )
        session.add(run)
        await session.flush()
        session.add(RunStep(run_id=run.id, seq=1, agent="echo_ops", kind="llm", model_tier="ultra"))
        session.add(RunStep(run_id=run.id, seq=2, agent="echo_ops", kind="llm", model_tier="nano"))
        await session.commit()
        budget = await RunBudget.load(session, run, MemoryUsageStore(), _settings())
    assert (budget.spent_usd, budget.tokens, budget.ultra_calls) == (Decimal("0.12"), 350, 1)
    assert budget.ultra_equiv_usd == Decimal("0.3")
    assert budget.incident_ultra_calls is None
