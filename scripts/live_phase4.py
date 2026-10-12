"""Phase 4 live verification: GroundsCrew-AI/legacy-flask-app from GitHub to a live URL through
POST /deploys, on kind and/or AWS ECS, with the results table for docs/deploy-live.md.

    scripts/live_phase4.sh kind                  # or: ecs, or: kind ecs
    LOGIN=<github login> SELF_APPROVE=1 scripts/live_phase4.sh kind ecs

For each target it starts a deploy through the API (the running dev stack), follows it, and at
the plan approval either waits for a person to click Approve on the Deploy page or, with
SELF_APPROVE=1, approves it itself through the dev-only route (APP_ENV=dev and
LIVE_SCRIPT_SELF_APPROVE=1 on the API; recorded as "script:live-phase4 (dev)"). Then it checks
the live URL and the deployed resource on the Map, and records run id, live URL, build
attempts, automatic fix, model calls, cost, and duration.

ECS (real AWS writes; only with an OK in the chat): after the checks, the deploy is rolled
back as an emergency action (a first deploy: the service is deleted) with its own approval,
and `make cleanup-deploys HOURS=0 APPLY=1 RUN=<run>` removes what that run left. The account
is checked first (881050425883, us-east-2).

DATABASE_URL is a random dummy secret: it is never printed or written to the report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import httpx  # noqa: E402
import jwt  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models import User, Workspace  # noqa: E402
from app.db.session import get_session_factory  # noqa: E402
from app.security.auth import ALGORITHM, API_AUDIENCE, WEB_ISSUER  # noqa: E402

ACCOUNT = "881050425883"
REPO = "GroundsCrew-AI/legacy-flask-app"
SCRIPT = "live-phase4"
TIMEOUT_SECONDS = 30 * 60


def fail(message: str) -> NoReturn:
    print(f"error: {message}")
    raise SystemExit(1)


@dataclass
class Row:
    target: str
    run_id: str
    status: str
    live_url: str | None
    build_attempts: int
    automatic_fix: bool
    model_calls: int
    cost_usd: float
    duration_s: float
    health: str
    on_map: str
    approved_by: str
    after: str = ""


class Api:
    """The API as the signed-in user: a fresh 5-minute internal JWT on every request."""

    def __init__(self, base: str, user: User) -> None:
        self.base = base.rstrip("/")
        self.user = user
        self.http = httpx.Client(base_url=self.base, timeout=30)

    def _headers(self) -> dict[str, str]:
        secret = get_settings().internal_jwt_secret
        if secret is None:
            fail("INTERNAL_JWT_SECRET is not set in .env.")
        now = int(time.time())
        claims = {
            "iss": WEB_ISSUER,
            "aud": API_AUDIENCE,
            "sub": str(self.user.github_id),
            "login": self.user.github_login or "",
            "name": self.user.name,
            "email": self.user.email,
            "iat": now,
            "exp": now + 300,
        }
        token = jwt.encode(claims, secret.get_secret_value(), algorithm=ALGORITHM)
        return {"Authorization": f"Bearer {token}"}

    def get(self, path: str, **params: Any) -> Any:
        r = self.http.get(path, params=params or None, headers=self._headers())
        if r.status_code >= 400:
            fail(f"GET {path}: {r.status_code} {r.text[:300]}")
        return r.json()

    def post(self, path: str, body: dict[str, Any]) -> Any:
        r = self.http.post(path, json=body, headers=self._headers())
        if r.status_code >= 400:
            fail(f"POST {path}: {r.status_code} {r.text[:300]}")
        return r.json()


async def _user(login: str) -> tuple[User, Workspace]:
    async with get_session_factory()() as session:
        user = await session.scalar(select(User).where(User.github_login == login))
        if user is None or user.github_id is None:
            fail(f"No user {login!r} has signed in yet.")
        workspace = await session.scalar(
            select(Workspace).where(Workspace.owner_id == user.id).order_by(Workspace.created_at)
        )
        if workspace is None:
            fail(f"{login} owns no workspace.")
        return user, workspace


def _pick(conns: list[dict[str, Any]], target: str) -> tuple[dict[str, Any], dict[str, Any]]:
    github = [c for c in conns if c["kind"] == "github" and c["status"] == "connected"]
    if not github:
        fail("No connected GitHub connection.")
    if target == "kind":
        found = [c for c in conns if c["kind"] == "k8s" and c["status"] == "connected"]
    else:
        found = [c for c in conns if c["kind"] == "aws" and c.get("tier") == "deploy"]
    if not found:
        fail(f"No {target} target connection (k8s, or AWS with Tier=deploy).")
    return github[0], found[0]


def _wait(api: Api, deploy_id: str, until: set[str]) -> dict[str, Any]:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    last = ""
    while time.monotonic() < deadline:
        d = api.get(f"/deploys/{deploy_id}")
        now = f"{d['status']} / {d['stage']}"
        if now != last:
            print(f"  {time.strftime('%H:%M:%S')} {now}", flush=True)
            last = now
        if d["status"] in until:
            return dict(d)
        time.sleep(5)
    fail(f"deploy {deploy_id} did not reach {until} in {TIMEOUT_SECONDS // 60} min")
    return {}


def _health(url: str | None, target: str) -> str:
    if not url:
        return "no URL"
    try:
        r = httpx.get(f"{url.rstrip('/')}/health", timeout=10)
        return f"/health {r.status_code}"
    except httpx.HTTPError as exc:
        return f"/health {type(exc).__name__}"


def deploy(
    api: Api, workspace: Workspace, target: str, self_approve: bool
) -> tuple[Row, dict[str, Any]]:
    conns = api.get("/connections", workspace_id=str(workspace.id))
    github, conn = _pick(conns, target)
    dummy = "postgresql://gc:" + secrets.token_urlsafe(18) + "@db.invalid:5432/app"
    started = time.monotonic()
    created = api.post(
        "/deploys",
        {
            "workspace_id": str(workspace.id),
            "repo": {"connection_id": github["id"], "repo": REPO, "ref": "main"},
            "target_connection_id": conn["id"],
            "env": [{"name": "DATABASE_URL", "value": dummy, "secret": True}],
        },
    )
    deploy_id = created["deploy_id"]
    print(f"{target}: deploy {deploy_id} to {conn['name']}")
    print(f"  follow it: http://localhost:3000/deploy/{deploy_id}")
    d = _wait(api, deploy_id, {"waiting_approval", "live", "failed", "stopped", "rolled_back"})
    approved_by = "-"
    if d["status"] == "waiting_approval":
        run = api.get(f"/runs/{d['run_id']}")
        pending = [a for a in run["approvals"] if a["status"] == "pending"]
        if not pending:
            fail("waiting for approval, but no pending approval")
        plan = d["plan"] or {}
        print(
            f"  plan: {', '.join(r['action'] + ' ' + r['kind'] for r in plan.get('resources', []))}"
        )
        if self_approve:
            done = api.post(f"/approvals/{pending[0]['id']}/self-approve", {"script": SCRIPT})
            approved_by = done["decided_via"]
            print(f"  approved by {approved_by}")
        else:
            print("  Approve the plan on the Deploy page; waiting...")
        d = _wait(api, deploy_id, {"live", "failed", "stopped", "rolled_back"})
        if not self_approve:
            run = api.get(f"/runs/{d['run_id']}")
            decided = [a for a in run["approvals"] if a["status"] == "approved"]
            approved_by = (
                decided[0]["decided_by_login"] or decided[0]["decided_by_name"] if decided else "-"
            )
    return _row_for(api, d, target, time.monotonic() - started, approved_by), d


def _row_for(api: Api, d: dict[str, Any], target: str, seconds: float, approved_by: str) -> Row:
    run = api.get(f"/runs/{d['run_id']}")
    steps = run["steps"]
    if approved_by == "-":
        decided = [a for a in run["approvals"] if a["status"] == "approved"]
        if decided:
            approved_by = decided[0]["decided_by_login"] or decided[0]["decided_by_name"] or "-"
    live = next((s["data"] for s in steps if s["kind"] == "live_url"), None)
    on_map = "-"
    if live and live.get("resource_id"):
        resource = api.get(f"/resources/{live['resource_id']}")
        on_map = f"{resource['type']} {resource['name']} ({resource['coverage']})"
    return Row(
        target=target,
        run_id=d["run_id"],
        status=d["status"],
        live_url=d["live_url"],
        build_attempts=d["build_attempts"],
        automatic_fix=d["automatic_fix"],
        model_calls=sum(1 for s in steps if s["kind"] == "llm"),
        cost_usd=float(d["cost_usd"] or 0),
        duration_s=seconds or float((d.get("duration_ms") or 0) / 1000),
        health=_health(d["live_url"], target),
        on_map=on_map,
        approved_by=approved_by,
    )


async def ecs_rollback_and_cleanup(row: Row, detail: dict[str, Any]) -> str:
    """Roll back the live ECS deploy (a first deploy: delete the service) as an emergency
    action with its own approval on the deploy's run, then clean up what the run left."""
    from cleanup_deploys import platform_account

    from app.connectors.credentials import ConnectionCredentialsProvider
    from app.db.models import Approval, ApprovalStatus, Connection, ToolPolicy
    from app.policy.actions import TARGET_ROLLBACK, issue_approved_action
    from app.policy.self_approve import script_label, self_approve_allowed
    from app.targets import target_for
    from app.targets.base import Deployment

    settings = get_settings()
    if not self_approve_allowed(settings):
        return "rollback skipped: needs APP_ENV=dev and LIVE_SCRIPT_SELF_APPROVE=1"
    if platform_account(settings) != ACCOUNT:
        return f"rollback skipped: platform identity not in {ACCOUNT}"
    record = detail["deploy"]
    sf = get_session_factory()
    async with sf() as session:
        conn = await session.get(Connection, uuid.UUID(record["connection_id"]))
        if conn is None:
            return "rollback skipped: the connection is gone"
        provider = ConnectionCredentialsProvider([conn])
        target = target_for(settings, provider, conn)
        deployment = Deployment(
            target="aws_ecs",
            connection_id=conn.id,
            run_id=uuid.UUID(row.run_id),
            app=record["app"],
            image=record["image"],
            revision=record["revision"],
            previous_revision=record["previous_revision"],
            location=f"{settings.gc_ecs_cluster}/{record['app']}",
            port=8000,
            health_path="/health",
        )
        plan = await target.rollback_plan(deployment)
        approval = Approval(
            run_id=deployment.run_id,
            workspace_id=conn.workspace_id,
            tool_call_id=f"live-rollback-{uuid.uuid4().hex[:8]}",
            tool_name=TARGET_ROLLBACK,
            policy=ToolPolicy.emergency,
            arguments={"plan_hash": plan.plan_hash(), "app": plan.app, "why": "live check"},
            status=ApprovalStatus.approved,
            decided_via=script_label(SCRIPT),
        )
        session.add(approval)
        await session.commit()
        token = await issue_approved_action(
            session,
            approval,
            run_id=deployment.run_id,
            action=TARGET_ROLLBACK,
            plan_hash=plan.plan_hash(),
        )
    target._session_factory = sf  # type: ignore[attr-defined]
    done = await target.rollback(plan, token)
    step = plan.steps[0]
    print(f"  rollback: {done.status} ({step.action} {step.kind} {step.name})")
    out = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "make",
            "cleanup-deploys",
            "HOURS=0",
            "APPLY=1",
            f"CONNECTION={conn.id}",
            f"RUN={row.run_id}",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    deleted = next(
        (line for line in out.stdout.splitlines() if line.startswith("Deleted ")), out.stdout[-300:]
    )
    print(f"  cleanup: {deleted}")
    return f"rolled back ({done.status}); cleanup: {deleted}"


async def run(args: argparse.Namespace) -> int:
    """One event loop for the whole run (the database engine is bound to it)."""
    user, workspace = await _user(args.login)
    api = Api(args.api, user)
    rows: list[Row] = []
    if args.rollback:
        detail = api.get(f"/deploys/{args.rollback}")
        row = _row_for(api, detail, "ecs", 0.0, "-")
        row.after = await ecs_rollback_and_cleanup(row, detail)
        rows.append(row)
    for target in args.targets:
        row, detail = await asyncio.to_thread(deploy, api, workspace, target, args.self_approve)
        if target == "ecs" and row.status == "live":
            row.after = await ecs_rollback_and_cleanup(row, detail)
        rows.append(row)
    header = (
        "| Target | Run | Status | Live URL | Health | Build attempts | Automatic fix "
        "| Model calls | Cost | Duration | On the Map | Approved by |"
    )
    print("\n" + header + "\n" + "|---" * 12 + "|")
    for r in rows:
        cells = [
            r.target,
            f"`{r.run_id}`",
            r.status,
            r.live_url or "-",
            r.health,
            str(r.build_attempts),
            "yes" if r.automatic_fix else "no",
            str(r.model_calls),
            f"${r.cost_usd:.4f}",
            f"{r.duration_s / 60:.1f} min",
            r.on_map,
            r.approved_by,
        ]
        print("| " + " | ".join(cells) + " |")
    for r in rows:
        if r.after:
            print(f"{r.target}: {r.after}")
    args.report.parent.mkdir(exist_ok=True)
    args.report.write_text(json.dumps([r.__dict__ for r in rows], indent=2))
    print(f"Report: {args.report.relative_to(REPO_ROOT)}")
    return 0 if rows and all(r.status == "live" for r in rows) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("targets", nargs="*", choices=["kind", "ecs"])
    parser.add_argument("--login", default=os.environ.get("LOGIN", "Rizwan-095"))
    parser.add_argument("--api", default=os.environ.get("API_URL", "http://localhost:8000"))
    parser.add_argument(
        "--self-approve", action="store_true", default=os.environ.get("SELF_APPROVE") == "1"
    )
    parser.add_argument(
        "--rollback", metavar="DEPLOY_ID", help="roll back and clean up a live ECS deploy"
    )
    parser.add_argument("--report", type=Path, default=REPO_ROOT / ".smoke" / "live-phase4.json")
    args = parser.parse_args(argv)
    if not args.targets and not args.rollback:
        parser.error("name a target (kind, ecs) or --rollback DEPLOY_ID")
    os.chdir(REPO_ROOT)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
