"""GitHub reader: commits and workflow runs in the window, through the GitHub App (GET only).

Each commit line names the files it changed (up to MAX_FILES), so Medic can tie a broken
service to the commit that touched its task definition or a migration. Commits and runs are
also change markers for the timeline chart.
"""

from datetime import datetime, timedelta
from typing import Any

from app.agents.medic.events import ChangeMarker
from app.connectors.github import GitHubApp, check_repo_name
from app.telemetry.evidence import ReaderOutput, line, parse_time

SOURCE = "github"
MAX_COMMITS = 15
MAX_RUNS = 20
MAX_FILES = 12
# A commit pushed a little before the window still explains an incident inside it (the deploy
# takes a few minutes), so commits are read from this much earlier.
LOOKBACK = timedelta(minutes=30)


def _iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


async def read_repo(
    app: GitHubApp, installation_id: int, repo: str, since: datetime, until: datetime
) -> ReaderOutput:
    full = check_repo_name(repo)
    out = ReaderOutput()
    commits: list[dict[str, Any]] = await app.get(
        installation_id,
        f"/repos/{full}/commits",
        {"since": _iso(since - LOOKBACK), "until": _iso(until), "per_page": MAX_COMMITS},
    )
    out.truncated = len(commits) >= MAX_COMMITS
    for c in commits:
        sha = str(c.get("sha", ""))
        info = c.get("commit") or {}
        at = parse_time((info.get("committer") or {}).get("date"))
        if not sha or at is None:
            continue
        detail: dict[str, Any] = await app.get(installation_id, f"/repos/{full}/commits/{sha}")
        files = [str(f.get("filename")) for f in detail.get("files", [])]
        shown = ", ".join(files[:MAX_FILES]) + (
            f" (+{len(files) - MAX_FILES} more)" if len(files) > MAX_FILES else ""
        )
        subject = str(info.get("message", "")).split("\n", 1)[0]
        author = (info.get("author") or {}).get("name") or "?"
        out.lines.append(
            line(
                at,
                SOURCE,
                full,
                f"commit {sha[:7]} by {author}: {subject} | files: {shown or 'none'}",
            )
        )
        out.changes.append(
            ChangeMarker(
                kind="commit",
                ref=sha,
                at=at,
                label=f"{sha[:7]} {subject}"[:120],
                url=c.get("html_url"),
            )
        )
    runs: dict[str, Any] = await app.get(
        installation_id,
        f"/repos/{full}/actions/runs",
        {"created": f"{_iso(since - LOOKBACK)}..{_iso(until)}", "per_page": MAX_RUNS},
    )
    for r in runs.get("workflow_runs", []):
        at = parse_time(r.get("updated_at")) or parse_time(r.get("created_at"))
        created = parse_time(r.get("created_at"))
        if at is None or created is None:
            continue
        path = str(r.get("path") or r.get("name") or "?").rsplit("/", 1)[-1]
        out.lines.append(
            line(
                at,
                SOURCE,
                full,
                f"workflow run {path} #{r.get('run_number', r.get('id'))} ({r.get('event')}) "
                f"{r.get('status')} {r.get('conclusion') or '-'} "
                f"at {str(r.get('head_sha', ''))[:7]}",
            )
        )
        out.changes.append(
            ChangeMarker(
                kind="deploy" if "deploy" in path.lower() else "other",
                ref=str(r.get("id")),
                at=created,
                label=f"{path} {r.get('conclusion') or r.get('status')}",
                url=r.get("html_url"),
            )
        )
    if not out.lines:
        # Evidence too: a config change, not code, is then the likelier cause.
        out.lines.append(
            line(until, SOURCE, full, "no commits or workflow runs in the incident window")
        )
    return out
