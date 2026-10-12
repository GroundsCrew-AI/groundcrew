"""Dry run of Medic's emergency actions: print the plans, read-only.

    make plan-emergency CONNECTION=<aws connection id> [CLUSTER=checkout] \
        [SERVICE=checkout-worker] [SECRET=<secret ARN>]

Uses the connection's groundcrew-operate role (EnableOperate=true) through the guarded operate
session, and only reads: DescribeServices, ListTaskDefinitions, DescribeSecret. Nothing is
executed; a real action needs an approval in Groundcrew. Refuses any account but 881050425883.
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

ACCOUNT = "881050425883"


async def run(args: argparse.Namespace) -> int:
    from app.actions.aws import AwsEmergencyActions
    from app.actions.base import EmergencyActionError
    from app.db.models import Connection
    from app.db.session import get_session_factory

    sf = get_session_factory()
    async with sf() as db:
        conn = await db.get(Connection, args.connection)
    if conn is None or conn.kind != "aws":
        print(f"error: no AWS connection {args.connection}")
        return 1
    if str(conn.config.get("account_id")) != ACCOUNT:
        print(f"error: connection is in {conn.config.get('account_id')}, not {ACCOUNT}. Stopping.")
        return 1
    actions = AwsEmergencyActions(sf)
    cluster = args.cluster
    print(f"DRY RUN (read-only, operate role): {conn.name}")
    plans = []
    try:
        plans.append(await actions.plan_ecs_rollback_service(conn.id, cluster, args.service))
    except EmergencyActionError as exc:
        print(f"  ecs_rollback_service: not possible: {exc}")
    try:
        plans.append(await actions.plan_force_redeploy(conn.id, cluster, args.service))
    except EmergencyActionError as exc:
        print(f"  force_redeploy: not possible: {exc}")
    if args.secret:
        try:
            plans.append(await actions.plan_secret_rollback_version(conn.id, args.secret))
        except EmergencyActionError as exc:
            print(f"  secret_rollback_version: not possible: {exc}")
    for plan in plans:
        print(f"  {plan.action}: {plan.summary}")
        after = plan.desired or "(new deployment)"
        print(f"      {plan.current or '-'} -> {after}  hash {plan.plan_hash()[:12]}")
    return 0 if plans else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--connection", required=True, type=uuid.UUID)
    parser.add_argument("--cluster", default="checkout")
    parser.add_argument("--service", default="checkout-worker")
    parser.add_argument("--secret", default=None)
    args = parser.parse_args(argv)
    os.chdir(REPO_ROOT)  # Settings reads .env from here
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
