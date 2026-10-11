"""Delete what Groundcrew deploys left behind: resources tagged project=groundcrew and older
than --hours, on AWS (every deploy-tier connection, or --connection) and in kind.

    make cleanup-deploys HOURS=24                      # dry run: list and decide, read only
    make cleanup-deploys HOURS=24 APPLY=1              # delete what the dry run lists as DELETE
    make cleanup-deploys HOURS=24 CONNECTION=<id>      # one AWS connection only

AWS calls use only ConnectionCredentialsProvider.aws_deploy_session() for each connection
(Groundcrew's platform identity assumes groundcrew-deploy; never a profile or ~/.aws). A dry
run works on a read-only copy of that session. kind uses the groundcrew-deployer kubeconfig
(.kind/groundcrew-deployer.host.kubeconfig), never cluster-admin. What is deleted and what is
never touched: app/connectors/deploy_cleanup.py and docs/phase4-infra.md.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

import boto3  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402
from app.connectors import deploy_cleanup as dc  # noqa: E402

KUBECONFIG = REPO_ROOT / ".kind" / "groundcrew-deployer.host.kubeconfig"


@dataclass
class Target:
    """One place cleanup looks: an AWS connection or the kind cluster."""

    label: str
    resources: list[dc.Resource] = field(default_factory=list)
    error: str = ""


def platform_account(settings: Settings) -> str:
    """The account of Groundcrew's platform identity (sts:GetCallerIdentity, home region).
    Checked before any role is assumed."""
    from app.connectors.aws import AwsSessions

    sessions = AwsSessions(settings)
    sts = sessions.base().client("sts", region_name=sessions.home_region)
    return str(sts.get_caller_identity()["Account"])


async def _deploy_sessions(
    connection_id: uuid.UUID | None, expect_account: str
) -> list[tuple[str, str, boto3.Session | None, str]]:
    """(label, account, deploy session or None, error) per deploy-tier AWS connection. A
    connection in another account than expect_account is never assumed."""
    from sqlalchemy import select

    from app.connectors.credentials import ConnectionCredentialsProvider
    from app.db.models import Connection
    from app.db.session import get_session_factory

    async with get_session_factory()() as db:
        query = select(Connection).where(Connection.kind == "aws")
        if connection_id:
            query = query.where(Connection.id == connection_id)
        conns = list((await db.execute(query)).scalars())
    if connection_id and not conns:
        raise SystemExit(f"error: no AWS connection {connection_id}")
    out = []
    for conn in conns:
        if not connection_id and (conn.config.get("tier") or "observe") != "deploy":
            continue
        account = str(conn.config.get("account_id") or "")
        label = f"AWS: {conn.name}" + ("" if account in conn.name else f" ({account})")
        if expect_account and account != expect_account:
            out.append((label, account, None, f"skipped: account {account}, not {expect_account}"))
            continue
        try:
            provider = ConnectionCredentialsProvider([conn])
            session = await asyncio.to_thread(provider.aws_deploy_session, conn.id)
            out.append((label, account, session, ""))
        except Exception as exc:  # one clear line per connection, never a traceback
            out.append((label, account, None, f"{type(exc).__name__}: {exc}"))
    return out


def _check_identity(session: boto3.Session, account: str, settings: Settings) -> str:
    """The session must be the deploy role in the connection's account and region."""
    ident = session.client("sts").get_caller_identity()
    if account and ident["Account"] != account:
        return f"the deploy session is in account {ident['Account']}, not {account}"
    if ":assumed-role/groundcrew-deploy/" not in str(ident["Arn"]):
        return f"the session is {ident['Arn']}, not the groundcrew-deploy role"
    if session.region_name != settings.gc_aws_region:
        return f"the session is in {session.region_name}, not {settings.gc_aws_region}"
    return ""


def _aws(
    connection_id: uuid.UUID | None,
    expect_account: str,
    settings: Settings,
    older: timedelta,
    now: datetime,
    apply: bool,
) -> list[Target]:
    targets = []
    for label, account, session, error in asyncio.run(
        _deploy_sessions(connection_id, expect_account)
    ):
        target = Target(label, error=error)
        targets.append(target)
        if session is None:
            continue
        try:
            reader = dc.read_only_copy(session)
            target.error = _check_identity(reader, account, settings)
            if target.error:
                continue
            target.resources = dc.find_aws(reader, settings, older_than=older, now=now)
            if apply:
                dc.apply_aws(session, settings, target.resources)
        except Exception as exc:
            target.error = f"{type(exc).__name__}: {exc}"[:300]
    return targets


def _kind(
    kubeconfig: Path, settings: Settings, older: timedelta, now: datetime, apply: bool
) -> Target:
    target = Target("kind (groundcrew-deployer)")
    if not kubeconfig.exists():
        target.error = f"skipped: no {kubeconfig.relative_to(REPO_ROOT)} (make kind-kubeconfig)"
        return target
    try:
        namespaces = dc.KubeNamespaces(str(kubeconfig))
        target.resources = dc.find_kind(namespaces, settings, older_than=older, now=now)
        if apply:
            dc.apply_kind(namespaces, target.resources)
    except Exception as exc:  # cluster down, kubeconfig stale: say so, keep going
        target.error = f"skipped: {type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
    return target


def render(targets: list[Target], now: datetime) -> list[str]:
    lines = []
    for t in targets:
        lines.append(f"\n{t.label}")
        if t.error:
            lines.append(f"  {t.error}")
        if not t.resources and not t.error:
            lines.append("  nothing tagged project=groundcrew")
        for r in t.resources:
            action = r.result or ("DELETE" if r.delete else "keep (pinned)" if r.pinned else "keep")
            run = r.run_id or "-"
            lines.append(
                f"  {action:<13} {r.kind:<15} {r.name:<46} {run:<28} {r.age(now):>7}  {r.note}"
            )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hours", required=True, type=float, help="only resources older than this")
    parser.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    parser.add_argument(
        "--connection", type=uuid.UUID, help="one AWS connection (default: all deploy-tier)"
    )
    parser.add_argument(
        "--account", default="", help="refuse to run unless the platform identity is in it"
    )
    parser.add_argument("--kubeconfig", type=Path, default=KUBECONFIG)
    args = parser.parse_args(argv)
    if args.hours < 0:
        parser.error("--hours must be 0 or more")
    os.chdir(REPO_ROOT)  # Settings reads .env from here
    settings = get_settings()
    now = datetime.now(UTC)
    older = timedelta(hours=args.hours)
    mode = "APPLY (deleting)" if args.apply else "DRY RUN (read only; APPLY=1 deletes)"
    print(f"{mode}: tagged project=groundcrew, older than {dc.format_age(older)}, "
          f"region {settings.gc_aws_region}, cluster {settings.gc_ecs_cluster}")  # fmt: skip
    if args.account:
        try:
            found = platform_account(settings)
        except Exception as exc:
            print(f"error: could not check the platform identity: {type(exc).__name__}: {exc}")
            return 1
        if found != args.account:
            print(f"error: the platform identity is in account {found}, not {args.account}.")
            return 1
        print(f"Platform identity: account {found} (checked before any role is assumed)")
    targets = _aws(args.connection, args.account, settings, older, now, args.apply)
    if not targets:
        print("\nNo AWS connection with tier deploy.")
    targets.append(_kind(args.kubeconfig, settings, older, now, args.apply))
    print("\n".join(render(targets, now)))
    planned = [r for t in targets for r in t.resources if r.delete]
    failed = [r for r in planned if r.result == "FAIL"]
    errors = [t for t in targets if t.error and not t.error.startswith("skipped")]
    if args.apply:
        done = sum(r.result == "PASS" for r in planned)
        print(f"\nDeleted {done} of {len(planned)}; {len(failed)} failed.")
    else:
        print(f"\nAPPLY=1 would delete {len(planned)} resource(s).")
    print("Task definitions are never deregistered (docs/phase4-infra.md).")
    return 1 if failed or errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
