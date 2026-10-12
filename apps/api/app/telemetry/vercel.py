"""Vercel reader: production deployments of one project in the window, with their state."""

from datetime import datetime

from app.agents.medic.events import ChangeMarker
from app.connectors.vercel import VercelClient
from app.telemetry.evidence import ReaderOutput, line, parse_time

SOURCE = "vercel"
MAX_DEPLOYMENTS = 20


async def read_project(
    client: VercelClient, project_id: str, name: str, since: datetime, until: datetime
) -> ReaderOutput:
    out = ReaderOutput()
    for d in await client.production_deployments(project_id, limit=MAX_DEPLOYMENTS):
        created = parse_time(d.get("created_at"))
        if created is None or not since <= created <= until:
            continue
        commit = str(d.get("commit_sha") or "")[:7]
        text = f"production deployment {d.get('url')} {d.get('state')}"
        if commit:
            text += f" from commit {commit}: {d.get('commit_message') or ''}".rstrip(": ")
        out.lines.append(line(created, SOURCE, name, text))
        out.changes.append(
            ChangeMarker(
                kind="deploy",
                ref=str(d.get("uid")),
                at=created,
                label=f"{name} deployment {d.get('state')}",
                url=f"https://{d['url']}" if d.get("url") else None,
            )
        )
    return out
