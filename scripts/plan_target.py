"""Dry run of a deploy target: print the plan and preflight for an app, read-only.

    make plan-target CONNECTION=<aws or k8s connection id> [APP=legacy-flask-app] [PORT=8000]

The target only reads (describe / list / get calls with the connection's deploy credentials):
no image is pushed and nothing is created. The image ID is a placeholder, because no sandbox
build ran; secret env vars get a placeholder value, so the plan shows them as "********".
Run this, show the plan, and get an OK before any live apply (docs/phase4-infra.md, Targets).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from app.config import get_settings  # noqa: E402
from app.targets.base import DeploySpec, EnvValue  # noqa: E402

PLACEHOLDER_IMAGE_ID = "sha256:" + "0" * 64  # no build ran


async def plan(args: argparse.Namespace) -> int:
    from app.connectors.credentials import ConnectionCredentialsProvider
    from app.db.models import Connection
    from app.db.session import get_session_factory
    from app.targets import target_for

    async with get_session_factory()() as db:
        conn = await db.get(Connection, args.connection)
    if conn is None:
        print(f"error: no connection {args.connection}")
        return 1
    provider = ConnectionCredentialsProvider([conn])
    target = target_for(get_settings(), provider, conn)
    spec = DeploySpec(
        run_id=uuid.uuid4(),
        connection_id=conn.id,
        app=args.app,
        source_image=f"gc-build/{args.app}:dry-run",
        source_image_id=PLACEHOLDER_IMAGE_ID,
        smoke_passed=True,
        tag=args.tag,
        port=args.port,
        health_path=args.health or None,
        env=[EnvValue(name=name, value="dry-run-placeholder", secret=True) for name in args.secret],
        required_env=args.secret,
    )
    result = await target.plan(spec)
    checks = await target.preflight(result)
    print(f"DRY RUN (read-only): {conn.name} ({conn.kind}), app {result.app}")
    print(f"\nImage: {result.image} (pushed only after approval)")
    print(f"Previous revision: {result.previous_revision or 'none (first deploy)'}")
    print("\nApply steps, in order:")
    for i, step in enumerate(result.steps, 1):
        detail = f"  ({step.detail})" if step.detail else ""
        print(f"  {i}. {step.action:<9} {step.kind:<22} {step.name}{detail}")
    print("\nPreflight:")
    for check in checks:
        print(f"  {check.status:<4} {check.name:<15} {check.detail}")
    print(f"\nRendered ({result.rendered_format}, secrets masked):\n{result.rendered}")
    return 0 if all(c.status == "pass" or c.overridden for c in checks) else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--connection", required=True, type=uuid.UUID)
    parser.add_argument("--app", default="legacy-flask-app")
    parser.add_argument("--tag", default="dry-run")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--health", default="/health")
    parser.add_argument("--secret", action="append", default=["DATABASE_URL"])
    args = parser.parse_args(argv)
    os.chdir(REPO_ROOT)  # Settings reads .env from here
    try:
        return asyncio.run(plan(args))
    except Exception as exc:  # one clear line, never a traceback with request data
        print(f"error: {type(exc).__name__}: {str(exc)[:300]}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
