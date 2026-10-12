"""Live Builder check (Phase 4, M4): Scout + Builder on GroundsCrew-AI/legacy-flask-app with real
Token Factory calls and the local Docker sandbox. Nothing is pushed or deployed.

    make live-builder [MAX_USD=0.10]

Expected: write_dockerfile gives a slim Python image, the build fails on psycopg2 (no
pg_config), fix_build adds the Postgres headers, the next build passes, and the smoke test
answers /health. Spend is recorded in llm_usage and capped by --max-usd.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from pydantic import SecretStr  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from app.agents.builder import Builder  # noqa: E402
from app.agents.scout.scout import scout  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.session import create_engine  # noqa: E402
from app.llm.budget import PgUsageStore, RunBudget  # noqa: E402
from app.llm.client import LLMResult, TokenFactory  # noqa: E402
from app.pipeline.events import BuildAttempt  # noqa: E402
from app.sandbox import DockerSandbox  # noqa: E402
from app.sandbox.base import SmokeEnv  # noqa: E402

REPO_URL = "https://github.com/GroundsCrew-AI/legacy-flask-app.git"
LOCAL_DB = "postgresql+asyncpg://groundcrew:groundcrew@localhost:5433/groundcrew"


class Recorder:
    def __init__(self) -> None:
        self.calls: list[LLMResult] = []
        self.last_seq: int | None = None

    async def __call__(self, result: LLMResult) -> None:
        self.calls.append(result)
        self.last_seq = len(self.calls)
        print(
            f"  model {result.task} on {result.model_id} ({result.reasoning_mode}"
            f"{', escalated' if result.escalated else ''}): {result.prompt_tokens} in / "
            f"{result.completion_tokens} out, ${result.cost_usd:.4f}, {result.latency_ms} ms",
            flush=True,
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-usd", type=float, default=0.10)
    parser.add_argument("--db-url", default=LOCAL_DB)
    args = parser.parse_args()
    os.chdir(REPO_ROOT)
    settings = get_settings().model_copy(
        update={"llm_run_usd_cap": args.max_usd, "sandbox_smoke_host": "localhost"}
    )
    if not settings.dev_mode_no_ultra:
        print("Set DEV_MODE_NO_ULTRA=true for this check.")
        return 1
    engine = create_engine(args.db_url)
    budget = RunBudget(usage=PgUsageStore(async_sessionmaker(engine)), settings=settings)
    llm = TokenFactory(settings)
    sandbox = DockerSandbox(settings)
    work = Path(tempfile.mkdtemp(prefix="gc-live-builder-"))
    run_id = "live-builder"
    recorder = Recorder()
    try:
        repo = work / "legacy-flask-app"
        subprocess.run(  # noqa: S603
            ["git", "clone", "--depth", "1", REPO_URL, str(repo)],  # noqa: S607
            check=True,
            capture_output=True,
        )
        found = await scout(repo, llm, budget, on_call=recorder)
        spec = found.spec
        print(
            f"Scout: {spec.language} {spec.framework}, port {spec.port}, health {spec.health_path}"
        )

        async def on_attempt(a: BuildAttempt) -> None:
            what = "passed" if a.ok else f"failed at {a.failing_step}: {a.error_summary}"
            if not a.ok and a.failing_step is None:
                print("    Dockerfile starts: " + repr(a.dockerfile_diff[:160]))
            secs = a.duration_ms / 1000
            print(f"  attempt {a.attempt} ({a.dockerfile_source}) {what} in {secs:.0f} s")

        async def on_note(text: str) -> None:
            print(f"  {text}")

        outcome = await Builder(llm, sandbox).build(
            repo,
            spec,
            app="legacy-flask-app",
            tag="live",
            run_id=run_id,
            budget=budget,
            recorder=recorder,
            on_attempt=on_attempt,
            on_note=on_note,
        )
        print(
            f"Build: ok={outcome.ok}, automatic fix={outcome.automatic_fix}, "
            f"stop={outcome.stop_reason}, image {outcome.image_id}"
        )
        if outcome.ok:
            env = [SmokeEnv("DATABASE_URL", SecretStr("postgresql://x:y@db.invalid/app"), True)]
            smoke = await Builder(llm, sandbox).smoke(outcome, spec, env, run_id=run_id)
            print("Smoke: " + ", ".join(f"{c.path} {c.status_code}" for c in smoke.checks))
            print("Dockerfile:\n" + outcome.dockerfile)
        total = sum(float(c.cost_usd) for c in recorder.calls)
        print(f"{len(recorder.calls)} model calls, ${total:.4f}")
        return 0 if outcome.ok and outcome.automatic_fix else 1
    finally:
        await sandbox.cleanup(run_id)
        shutil.rmtree(work, ignore_errors=True)
        await llm.aclose()
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
