"""Live check of the ECS target with GroundsCrew-AI/legacy-flask-app (Phase 4, M3).

    make live-ecs-target CONNECTION=<aws connection id> CONFIRM=881050425883 APPROVED_BY=<login>

Real AWS writes in account 881050425883 / us-east-2, only after an OK in the chat:
build legacy-flask-app in the sandbox with the psycopg2 fix applied by hand (Builder lands in
M4), smoke-test it, plan, preflight, record the approval, apply with an ApprovedAction (push
first), verify, read the logs, then roll back the first deploy (an emergency approval: the
service is deleted). The image, log group, security group, and secret stay for
`make cleanup-deploys HOURS=0 APPLY=1 RUN=<run id>`, which the run prints at the end.

DATABASE_URL is a random dummy value: it is never printed (the app logs only a hash, and every
line goes through a scrubber that refuses to emit it). The approvals are recorded in the
dev database on a run in the connection's workspace, approved "in chat" by the user who
connected the account.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from app.config import get_settings  # noqa: E402

ACCOUNT = "881050425883"
REPO_URL = "https://github.com/GroundsCrew-AI/legacy-flask-app.git"
# The fix Builder should find on its own in M4 (docs/phase4-infra.md, Sandbox: live checks).
FIXED_DOCKERFILE = """FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev libpq-dev \\
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["gunicorn", "-b", "0.0.0.0:8000", "app:app"]
"""


class Scrub:
    def __init__(self, *values: str) -> None:
        self.values = [v for v in values if v]

    def __call__(self, text: str) -> str:
        for value in self.values:
            text = text.replace(value, "[redacted]")
        return text


@dataclass
class Row:
    step: str
    ok: bool
    seconds: float
    note: str


@dataclass
class Report:
    rows: list[Row] = field(default_factory=list)
    service_started: float | None = None
    service_deleted: float | None = None
    secret_created: float | None = None


async def run(args: argparse.Namespace) -> int:
    from cleanup_deploys import platform_account
    from pydantic import SecretStr
    from smoke_ecs_deploy import estimate_cost
    from sqlalchemy import select

    from app.connectors.credentials import ConnectionCredentialsProvider
    from app.db.models import Approval, ApprovalStatus, Connection, Run, RunStatus, ToolPolicy, User
    from app.db.session import get_session_factory
    from app.policy.actions import (
        TARGET_APPLY,
        TARGET_ROLLBACK,
        TargetAction,
        issue_approved_action,
    )
    from app.sandbox import DockerSandbox
    from app.sandbox.base import SmokeEnv
    from app.targets import EcsTarget, target_for
    from app.targets.base import Deployment, DeploySpec, EnvValue

    settings = get_settings()
    dummy = "postgresql://gc:" + secrets.token_urlsafe(18) + "@db.invalid:5432/app"
    scrub = Scrub(dummy)
    report = Report()

    def say(text: str) -> None:
        print(scrub(text), flush=True)

    async def step(name: str, coro: Awaitable[Any]) -> Any:
        start = time.monotonic()
        try:
            result = await coro
        except Exception as exc:
            report.rows.append(
                Row(
                    name,
                    False,
                    time.monotonic() - start,
                    scrub(f"{type(exc).__name__}: {exc}")[:300],
                )
            )
            raise
        note = result[1] if isinstance(result, tuple) else ""
        report.rows.append(Row(name, True, time.monotonic() - start, scrub(str(note))))
        say(f"  ok  {name}: {scrub(str(note))}")
        return result[0] if isinstance(result, tuple) else result

    # 0. Where we are: account and region, before anything else.
    found = await asyncio.to_thread(platform_account, settings)
    if found != ACCOUNT or settings.gc_aws_region != "us-east-2":
        say(
            f"error: platform identity in {found}, region {settings.gc_aws_region}; "
            f"expected {ACCOUNT}, us-east-2"
        )
        return 1
    sf = get_session_factory()
    async with sf() as db:
        conn = await db.get(Connection, args.connection)
        if conn is None or conn.kind != "aws":
            say(f"error: no AWS connection {args.connection}")
            return 1
        approver = await db.scalar(select(User).where(User.github_login == args.approved_by))
        if approver is None:
            say(f"error: no user {args.approved_by} (the person who approved in chat)")
            return 1
        if args.resume:
            existing = await db.get(Run, args.resume)
            if existing is None or existing.workspace_id != conn.workspace_id:
                say(f"error: no run {args.resume} in this connection's workspace")
                return 1
            run_id = existing.id
        else:
            run = Run(
                workspace_id=conn.workspace_id,
                agent="deploy",
                goal="M3 live ECS check: legacy-flask-app",
                status=RunStatus.running,
                started_at=datetime.now(UTC),
            )
            db.add(run)
            await db.commit()
            run_id = run.id
    provider = ConnectionCredentialsProvider([conn])
    target = target_for(settings, provider, conn)
    if not isinstance(target, EcsTarget):
        say(f"error: {conn.name} is not an ECS target")
        return 1
    target._session_factory = sf
    deploy_account = await target._account_id()
    if deploy_account != ACCOUNT:
        say(f"error: the deploy role is in {deploy_account}, not {ACCOUNT}")
        return 1
    say(f"LIVE RUN {run_id}: account {ACCOUNT}, us-east-2, cluster {settings.gc_ecs_cluster}")

    sandbox = DockerSandbox(settings.model_copy(update={"sandbox_smoke_host": "localhost"}))
    work = Path(tempfile.mkdtemp(prefix="gc-live-ecs-"))
    deployment = None
    approved_by = args.approved_by
    try:

        async def approval(
            action: TargetAction, plan_hash: str, policy: ToolPolicy, what: str
        ) -> tuple[Any, str]:
            async with sf() as db:
                row = Approval(
                    run_id=run_id,
                    workspace_id=conn.workspace_id,
                    tool_call_id=f"live-{uuid.uuid4()}",
                    tool_name=action,
                    policy=policy,
                    arguments={"plan_hash": plan_hash, "what": what},
                    status=ApprovalStatus.approved,
                    decided_by=approver.id,
                    decided_at=datetime.now(UTC),
                )
                db.add(row)
                await db.commit()
                token = await issue_approved_action(
                    db, row, run_id=run_id, action=action, plan_hash=plan_hash
                )
            return token, f"approval {row.id} ({policy}), approved in chat by {approved_by}"

        if args.resume is None:
            # 1. Source and build (sandbox only; nothing leaves this machine yet).
            async def clone() -> tuple[str, str]:
                repo = work / "legacy-flask-app"
                out = await asyncio.to_thread(
                    subprocess.run,
                    ["git", "clone", "--depth", "1", REPO_URL, str(repo)],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                del out
                commit = subprocess.run(  # noqa: S603
                    ["git", "-C", str(repo), "rev-parse", "HEAD"],  # noqa: S607
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                (repo / "Dockerfile").write_text(FIXED_DOCKERFILE)
                return commit, f"commit {commit[:12]}, Dockerfile with the psycopg2 fix (by hand)"

            commit = await step("1 clone legacy-flask-app", clone())
            tag = commit[:12]
            ref = f"gc-live/legacy-flask-app:{tag}"

            async def build() -> tuple[str, str]:
                repo = work / "legacy-flask-app"
                result = await sandbox.build(repo, repo / "Dockerfile", ref, run_id=str(run_id))
                if not result.ok:
                    raise RuntimeError(
                        f"build failed at {result.failing_step}: {result.error_lines[-3:]}"
                    )
                image_id = subprocess.run(  # noqa: S603
                    ["docker", "image", "inspect", "--format", "{{.Id}}", ref],  # noqa: S607
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                return image_id, f"{image_id[:19]} in {result.duration_ms / 1000:.0f} s"

            image_id = await step("2 build in the sandbox", build())

            async def smoke() -> tuple[None, str]:
                env = [SmokeEnv("DATABASE_URL", SecretStr(dummy), secret=True)]
                result = await sandbox.run_smoke(
                    ref, 8000, env, run_id=str(run_id), health_path="/health"
                )
                if not result.ok:
                    raise RuntimeError(
                        f"smoke failed: {result.detail} {result.logs_tail or ''}"[:300]
                    )
                return None, ", ".join(f"{c.path} {c.status_code}" for c in result.checks)

            await step("3 smoke test in the sandbox", smoke())

            spec = DeploySpec(
                run_id=run_id,
                connection_id=conn.id,
                app="legacy-flask-app",
                source_image=ref,
                source_image_id=image_id,
                smoke_passed=True,
                tag=tag,
                port=8000,
                health_path="/health",
                env=[EnvValue(name="DATABASE_URL", value=dummy, secret=True)],
                required_env=["DATABASE_URL"],
                repo="GroundsCrew-AI/legacy-flask-app",
                commit=commit,
            )

            # 2. Plan and preflight (read only).
            async def plan_and_check() -> tuple[Any, str]:
                plan = await target.plan(spec)
                checks = await target.preflight(plan)
                failed = [c for c in checks if c.status == "fail" and not c.overridden]
                if failed:
                    raise RuntimeError(
                        "preflight: " + "; ".join(f"{c.name}: {c.detail}" for c in failed)
                    )
                say(
                    "      "
                    + "\n      ".join(
                        f"{i}. {s.action} {s.kind} {s.name}" for i, s in enumerate(plan.steps, 1)
                    )
                )
                return (
                    plan,
                    f"{len(plan.steps)} steps, preflight all pass, hash {plan.plan_hash()[:12]}",
                )

            plan = await step("4 plan and preflight (read only)", plan_and_check())

            token = await step(
                "5 approval (target_apply, write)",
                approval(
                    TARGET_APPLY,
                    plan.plan_hash(),
                    ToolPolicy.write,
                    "deploy legacy-flask-app to ECS",
                ),
            )

            # 3. Apply: ECR repository, push, then the rest.
            async def apply() -> tuple[Any, str]:
                report.secret_created = time.monotonic()
                dep = await target.apply(plan, spec, token, sandbox)
                report.service_started = time.monotonic()
                return (
                    dep,
                    f"task definition {dep.revision.rsplit('/', 1)[-1] if dep.revision else '?'}",
                )

            deployment = await step("6 apply (push first, token spent)", apply())

            async def reuse() -> tuple[Any, str]:
                try:
                    await target.apply(plan, spec, token, sandbox)
                except Exception as exc:
                    return None, f"refused: {str(exc)[:90]}"
                raise RuntimeError("the spent token ran apply again")

            await step("7 same token again", reuse())

        else:
            # Resume after a stop at verify: the service is already there (no new writes).
            name = "legacy-flask-app"
            live_service = await target._service(name)
            if live_service is None:
                raise RuntimeError(f"no ACTIVE service {name} to resume")
            report.service_started = (
                time.monotonic() - (datetime.now(UTC) - live_service["createdAt"]).total_seconds()
            )
            report.secret_created = report.service_started - 30
            definition = await target._aws(
                "ecs", "describe_task_definition", taskDefinition=live_service["taskDefinition"]
            )
            deployment = Deployment(
                target="aws_ecs",
                connection_id=conn.id,
                run_id=run_id,
                app=name,
                image=definition["taskDefinition"]["containerDefinitions"][0]["image"],
                revision=live_service["taskDefinition"],
                previous_revision=None,
                location=f"{settings.gc_ecs_cluster}/{name}",
                port=8000,
                health_path="/health",
                external_id=live_service["serviceArn"],
            )
            say(f"  resumed at verify: {live_service['taskDefinition'].rsplit('/', 1)[-1]}")

        async def verify() -> tuple[Any, str]:
            health = await target.verify(deployment)
            checks = ", ".join(f"{c.name} {c.detail}" for c in health.checks)
            if not health.ok:
                raise RuntimeError(f"{checks}; logs: {health.logs_tail or ''}"[:300])
            return health, f"{health.live_url}: {checks}"

        health = await step("8 verify (rollout, /health, /)", verify())
        live_url = health.live_url

        async def logs() -> tuple[Any, str]:
            lines = await target.logs(deployment, datetime.now(UTC) - timedelta(minutes=15))
            if any(dummy in line.message or "postgresql://" in line.message for line in lines):
                raise RuntimeError("a database URL appeared in the logs")
            return None, f"{len(lines)} log lines, no database URL in them"

        await step("9 logs (CloudWatch)", logs())

        # 4. Roll back the first deploy: an emergency approval; the service is deleted.
        rollback = await target.rollback_plan(deployment)
        rtoken = await step(
            "10 approval (target_rollback, emergency)",
            approval(
                TARGET_ROLLBACK,
                rollback.plan_hash(),
                ToolPolicy.emergency,
                "roll back the first deploy: delete the service",
            ),
        )

        async def roll() -> tuple[Any, str]:
            done = await target.rollback(rollback, rtoken)
            report.service_deleted = time.monotonic()
            return done, f"{done.status}: {rollback.steps[0].kind} {rollback.steps[0].name}"

        await step("11 rollback (first deploy: delete the service)", roll())
        async with sf() as db:
            stored = await db.get(Run, run_id)
            if stored is not None:
                stored.status = RunStatus.succeeded
                stored.finished_at = datetime.now(UTC)
                await db.commit()
        del live_url
    except Exception as exc:
        say(f"\nSTOPPED: {scrub(type(exc).__name__ + ': ' + str(exc))[:400]}")
        if deployment is not None and report.service_deleted is None:
            say(
                "The service may still run: make cleanup-deploys HOURS=0 APPLY=1 RUN=" + str(run_id)
            )
    finally:
        await sandbox.cleanup(str(run_id))
        shutil.rmtree(work, ignore_errors=True)

    end = time.monotonic()
    service = (
        (report.service_deleted or end) - report.service_started if report.service_started else 0.0
    )
    secret = end - report.secret_created if report.secret_created else 0.0
    say("\n| Step | Result | Time | Note |\n|---|---|---|---|")
    for r in report.rows:
        say(f"| {r.step} | {'PASS' if r.ok else 'FAIL'} | {r.seconds:.1f} s | {r.note} |")
    cost = estimate_cost(service, secret + 300)
    say(
        f"\nRun {run_id}. Service ran {service:.0f} s; estimated cost ${cost:.5f} (us-east-2 "
        "list prices: Fargate 0.25 vCPU / 0.5 GB, public IPv4, the secret prorated with 5 min "
        "for cleanup)."
    )
    say(f"Next: make cleanup-deploys HOURS=0 APPLY=1 CONNECTION={conn.id} RUN={run_id}")
    return 0 if report.rows and all(r.ok for r in report.rows) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--connection", required=True, type=uuid.UUID)
    parser.add_argument(
        "--confirm", required=True, help=f"must be {ACCOUNT}: this makes real AWS writes"
    )
    parser.add_argument("--approved-by", required=True, help="GitHub login of who approved in chat")
    parser.add_argument(
        "--resume", type=uuid.UUID, help="continue a run that stopped after apply, from verify"
    )
    args = parser.parse_args(argv)
    if args.confirm != ACCOUNT:
        parser.error(f"--confirm must be {ACCOUNT}")
    os.chdir(REPO_ROOT)  # Settings reads .env from here
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
