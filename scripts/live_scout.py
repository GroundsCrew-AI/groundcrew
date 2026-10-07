"""Run Scout with the real Nemotron 3 Nano on every fixture. You run this; CI never does.

    make db && make migrate        # the spend caps are counted in the local llm_usage table
    uv run --project apps/api python scripts/live_scout.py [--max-usd 0.05] [fixture ...]

Prints, per fixture, field-level accuracy against expected_appspec.json for Scout (rules +
model) and for the model on its own, then a table of every model call: agent, model,
reasoning mode, tokens, cost, Ultra-equivalent cost, and the total saving.

Hard cap: --max-usd for the whole run (default $0.05), on top of LLM_DAILY_USD_CAP and
LLM_TOTAL_USD_CAP. A call is refused before it is sent once the cap is reached.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from app.agents.scout.accuracy import GOLDEN_FIELDS, field_matches, load_golden  # noqa: E402
from app.agents.scout.appspec import AppSpec  # noqa: E402
from app.agents.scout.scout import scout  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.session import create_engine  # noqa: E402
from app.llm.budget import BudgetExceededError, PgUsageStore, RunBudget  # noqa: E402
from app.llm.client import LLMResult, TokenFactory  # noqa: E402

FIXTURES = REPO_ROOT / "demo" / "fixtures"
LOCAL_DB = "postgresql+asyncpg://groundcrew:groundcrew@localhost:5433/groundcrew"


def _draft_spec(result: Any) -> AppSpec:
    """The model's answer alone, as an AppSpec, for its own accuracy."""
    draft = result.draft
    if draft is None:
        return AppSpec()
    values = draft.model_dump(exclude={"evidence", "env_vars"})
    env = [
        {"name": v.name, "required": bool(v.required), "secret": bool(v.secret)}
        for v in draft.env_vars
    ]
    return AppSpec(**values, env_vars=env, has_dockerfile=result.spec.has_dockerfile)


def _pct(matches: dict[str, bool]) -> str:
    return f"{100 * sum(matches.values()) / len(matches):.0f}%"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("fixtures", nargs="*", help="fixture names (default: all)")
    parser.add_argument("--max-usd", type=float, default=0.05, help="hard cap for this run")
    parser.add_argument("--db-url", default=LOCAL_DB, help="Postgres with the llm_usage table")
    args = parser.parse_args()

    names = args.fixtures or sorted(p.name for p in FIXTURES.iterdir() if p.is_dir())
    settings = get_settings().model_copy(update={"llm_run_usd_cap": args.max_usd})
    engine = create_engine(args.db_url)
    usage = PgUsageStore(async_sessionmaker(engine, expire_on_commit=False))
    budget = RunBudget(usage=usage, settings=settings)
    calls: list[tuple[str, LLMResult]] = []
    llm = TokenFactory(settings)
    print(f"Scout on {len(names)} fixtures with {settings.model_nano}; cap ${args.max_usd:.2f}\n")

    rows: list[tuple[str, dict[str, bool], dict[str, bool], AppSpec]] = []
    try:
        for name in names:
            fixture = FIXTURES / name

            async def record(result: LLMResult, fixture_name: str = name) -> None:
                calls.append((fixture_name, result))

            try:
                outcome = await scout(fixture / "repo", llm, budget, on_call=record)
            except BudgetExceededError as exc:
                print(f"Stopped at {name}: {exc}")
                break
            golden = load_golden(fixture)
            merged = field_matches(outcome.spec, golden)
            alone = field_matches(_draft_spec(outcome), golden)
            rows.append((name, merged, alone, outcome.spec))
            for note in outcome.notes:
                print(f"  {name}: {note}")
    finally:
        await llm.aclose()
        await engine.dispose()

    print("Field accuracy against expected_appspec.json (Scout = rules + model)\n")
    header = f"{'field':16}" + "".join(f"{n[:14]:>16}" for n, *_ in rows)
    print(header)
    for name in GOLDEN_FIELDS:
        cells = ""
        for _, merged, alone, _ in rows:
            scout_mark = "ok" if merged[name] else "WRONG"
            model_mark = "model ok" if alone[name] else "model x"
            cells += f"{scout_mark + ' / ' + model_mark:>16}"
        print(f"{name:16}{cells}")
    print(f"{'Scout':16}" + "".join(f"{_pct(m):>16}" for _, m, _, _ in rows))
    print(f"{'model alone':16}" + "".join(f"{_pct(a):>16}" for _, _, a, _ in rows))
    for name, _, _, spec in rows:
        if spec.conflicts or spec.low_confidence:
            conflicts = ", ".join(
                f"{c.field} (rule {c.rule_value}, model {c.model_value})" for c in spec.conflicts
            )
            print(f"\n{name}: confidence {spec.confidence}")
            if conflicts:
                print(f"  conflicts: {conflicts}")
            if spec.low_confidence:
                print(f"  low confidence: {', '.join(spec.low_confidence)}")

    print("\nModel calls\n")
    print(
        f"{'step':16}{'agent':8}{'model':40}{'reason':8}{'tokens':>8}{'cost $':>11}{'ultra $':>11}"
    )
    total = Decimal(0)
    ultra = Decimal(0)
    for fixture_name, r in calls:
        total += r.cost_usd
        ultra += r.ultra_equiv_usd
        model = r.model_id + (" (fallback)" if r.fell_back else "")
        print(
            f"{fixture_name[:15]:16}{'scout':8}{model[:39]:40}{r.reasoning_mode!s:8}"
            f"{r.total_tokens:>8}{float(r.cost_usd):>11.6f}{float(r.ultra_equiv_usd):>11.6f}"
        )
    saving = 100 * (1 - total / ultra) if ultra else Decimal(0)
    print(f"\nTotal: ${float(total):.6f} (all on Ultra: ${float(ultra):.6f}; saving {saving:.0f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
