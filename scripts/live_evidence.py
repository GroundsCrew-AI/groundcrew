"""Print what Medic would read for some resources, from the live demo account. Read-only.

    make live-evidence WORKSPACE=<id> RESOURCES="checkout-api checkout-worker" [MINUTES=60]

Runs collect_evidence (the same readers Medic uses) over the last MINUTES for graph resources
named in RESOURCES. Every AWS call goes through the connection's read-only role; nothing is
written and no secret value is read. Before any AWS call it checks that each AWS connection is
in account 881050425883 and stops otherwise. No model calls.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

ACCOUNT = "881050425883"


async def run(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from app.db.models import Connection, Resource
    from app.db.session import get_session_factory
    from app.telemetry.collect import collect

    sf = get_session_factory()
    async with sf() as db:
        rows = list(
            await db.scalars(
                select(Resource).where(
                    Resource.workspace_id == args.workspace, Resource.name.in_(args.resources)
                )
            )
        )
        aws = list(
            await db.scalars(
                select(Connection).where(
                    Connection.workspace_id == args.workspace, Connection.kind == "aws"
                )
            )
        )
    wrong = [c.name for c in aws if str(c.config.get("account_id")) != ACCOUNT]
    if wrong:
        print(f"error: AWS connections outside {ACCOUNT}: {', '.join(wrong)}. Stopping.")
        return 1
    if not rows:
        print("error: none of those resources are in this workspace's graph")
        return 1
    until = datetime.now(UTC)
    since = until - timedelta(minutes=args.minutes)
    evidence = await collect(sf, args.workspace, [r.id for r in rows], since, until)
    print(f"READ-ONLY evidence {since:%H:%M}-{until:%H:%M} UTC for {', '.join(args.resources)}")
    print(f"neighbors: {', '.join(r.name for r in evidence.neighbors) or 'none'}")
    for note in evidence.notes:
        print(f"note: {note}")
    for change in evidence.changes:
        print(f"change: {change.at:%H:%M:%S} {change.kind} {change.label}")
    for key, text in evidence.texts().items():
        if key == "timeline" and not args.timeline:
            continue
        cut = " (truncated)" if evidence.truncated.get(key) else ""
        print(f"\n== {key}{cut}\n{text}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", required=True, type=uuid.UUID)
    parser.add_argument("--resources", required=True, nargs="+")
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument("--timeline", action="store_true", help="also print the merged timeline")
    args = parser.parse_args(argv)
    os.chdir(REPO_ROOT)  # Settings reads .env from here
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
