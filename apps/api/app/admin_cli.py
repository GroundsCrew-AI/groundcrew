"""Operator commands on the server, against the database in DATABASE_URL.

    python -m app.admin_cli signups status|pause|resume
    python -m app.admin_cli quota show <workspace id>
    python -m app.admin_cli quota set <workspace id> <tavily|builds|concurrent_runs|connections> <n>
    python -m app.admin_cli quota clear <workspace id> <quota>
    python -m app.admin_cli example candidates <source workspace id>
    python -m app.admin_cli example build <source workspace id> [--incident <id>]... \\
        [--deploy <id>]... [--apply]

On the platform host: `docker compose exec api python -m app.admin_cli ...` (docs/operations.md).
Each change is in the audit log as done by "Groundcrew". `example build` copies the chosen work
into the read-only Example workspace (app.example); without --apply it only reports what it would
copy and rolls back.
"""

import asyncio
import sys
import uuid
from typing import get_args

from app import example, platform, quotas
from app.audit import record
from app.config import get_settings
from app.db.models import Workspace
from app.db.session import get_session_factory

USAGE = __doc__ or ""


async def _signups(action: str) -> str:
    async with get_session_factory()() as session:
        if action in ("pause", "resume"):
            paused = action == "pause"
            await platform.set_signups_paused(session, paused, None)
            record(
                session,
                workspace_id=None,
                actor=None,
                action="platform.signups_paused" if paused else "platform.signups_resumed",
            )
            await session.commit()
        elif action != "status":
            raise SystemExit(USAGE)
        state = "paused" if await platform.signups_paused(session) else "open"
        return (
            f"Sign-ups are {state}; {await platform.signups_last_hour(session)} in the last hour."
        )


async def _quota(action: str, workspace_id: str, name: str | None, value: str | None) -> str:
    settings = get_settings()
    async with get_session_factory()() as session:
        workspace = await session.get(Workspace, uuid.UUID(workspace_id))
        if workspace is None:
            raise SystemExit(f"No workspace {workspace_id}.")
        if action in ("set", "clear"):
            if name not in get_args(quotas.Quota):
                raise SystemExit(f"Quota is one of: {', '.join(get_args(quotas.Quota))}.")
            current = dict((workspace.settings or {}).get(quotas.QUOTAS_KEY) or {})
            if action == "set":
                if value is None or not value.isdigit():
                    raise SystemExit("Give the new limit as a whole number.")
                current[name] = int(value)
            else:
                current.pop(name, None)
            workspace.settings = {**(workspace.settings or {}), quotas.QUOTAS_KEY: current}
            record(
                session,
                workspace_id=workspace.id,
                actor=None,
                action="quota.changed",
                detail={"quota": name, "limit": current.get(name)},
            )
            await session.commit()
        elif action != "show":
            raise SystemExit(USAGE)
        used = await quotas.usage(session, workspace, settings)
        return "\n".join(f"{n}: {u.used} of {u.limit}" for n, u in used.items())


async def _example(action: str, source: str, rest: list[str]) -> str:
    source_id = uuid.UUID(source)
    async with get_session_factory()() as session:
        if action == "candidates":
            return await example.candidates(session, source_id)
        if action != "build":
            raise SystemExit(USAGE)
        incidents: list[uuid.UUID] = []
        deploys: list[uuid.UUID] = []
        apply = False
        args = iter(rest)
        for arg in args:
            if arg == "--apply":
                apply = True
            elif arg in ("--incident", "--deploy"):
                value = uuid.UUID(next(args, ""))
                (incidents if arg == "--incident" else deploys).append(value)
            else:
                raise SystemExit(USAGE)
        workspace, plan = await example.build(session, source_id, incidents, deploys)
        lines = [f"{table}: {n}" for table, n in plan.counts.items()]
        lines += [f"problem: {p}" for p in plan.problems]
        if plan.problems or not apply:
            await session.rollback()
            lines.append(
                "Not applied: fix the problems above."
                if plan.problems
                else "Dry run: nothing changed. Add --apply to copy."
            )
            return "\n".join(lines)
        record(
            session,
            workspace_id=workspace.id,
            actor=None,
            action="example.rebuilt",
            detail={"tables": plan.counts},
        )
        await session.commit()
        lines.append(f"Copied into the example workspace {workspace.id}.")
        return "\n".join(lines)


def main(argv: list[str]) -> str:
    if len(argv) == 2 and argv[0] == "signups":
        return asyncio.run(_signups(argv[1]))
    if len(argv) >= 3 and argv[0] == "quota":
        name = argv[3] if len(argv) > 3 else None
        value = argv[4] if len(argv) > 4 else None
        return asyncio.run(_quota(argv[1], argv[2], name, value))
    if len(argv) >= 3 and argv[0] == "example":
        return asyncio.run(_example(argv[1], argv[2], argv[3:]))
    raise SystemExit(USAGE)


if __name__ == "__main__":
    print(main(sys.argv[1:]))
