"""Follow-ups: the permanent fix an incident still needs after it resolved.

An emergency rollback can resolve an incident while its fix PR is still open. The repository's
default branch then still has the bad change, and the next deploy brings the outage back. So
when Medic resolves an incident with a fix PR that is not deployed yet, it records a follow-up
(`incident_followups`), and this module tracks it through the GitHub App, read-only:

- open -> merged when the PR is merged (incident memory may now reuse the fix), then
  merged -> deployed when a successful deploy workflow run (path containing "deploy") on the base
  branch, started after the merge, contains the merge commit. Done.
- open -> closed_unmerged when the PR is closed without merging (memory notes "rollback only").

`track_followups` runs on the watcher's schedule (`watch_incidents_job`). A PR is checked at most
every FOLLOWUP_CHECK_EVERY, and not after FOLLOWUP_TRACK_FOR. Every change records an
`incident_updated` step (status unchanged, `followups` set) on the incident's latest run.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.medic.events import (
    PENDING_FOLLOWUP_STATES,
    FollowUp,
    FollowUpState,
    IncidentStatus,
    IncidentUpdated,
)
from app.agents.medic.state import MedicState
from app.connectors.github import GitHubApp, GitHubError, check_repo_name
from app.db.models import Incident, IncidentFollowUp, Run
from app.db.session import SessionFactory
from app.steps import record_step
from app.telemetry.evidence import parse_time

logger = logging.getLogger("groundcrew.incidents")

FOLLOWUP_CHECK_EVERY: Final = timedelta(minutes=2)
FOLLOWUP_TRACK_FOR: Final = timedelta(days=7)
MAX_DEPLOY_RUNS = 20


def out(row: IncidentFollowUp) -> FollowUp:
    state: FollowUpState = row.state  # type: ignore[assignment]
    return FollowUp(
        id=row.id,
        kind="fix_pr",
        state=state,
        repo=row.repo,
        number=row.number,
        url=row.url,
        triggering_commit=row.triggering_commit,
        merge_commit=row.merge_commit,
        merged_at=row.merged_at,
        deployed_at=row.deployed_at,
        deploy_url=row.deploy_url,
        closed_at=row.closed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        tracked_until=row.created_at + FOLLOWUP_TRACK_FOR,
    )


async def for_incidents(
    session: AsyncSession, incident_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[FollowUp]]:
    found: dict[uuid.UUID, list[FollowUp]] = {i: [] for i in incident_ids}
    if not incident_ids:
        return found
    rows = await session.scalars(
        select(IncidentFollowUp)
        .where(IncidentFollowUp.incident_id.in_(incident_ids))
        .order_by(IncidentFollowUp.created_at, IncidentFollowUp.number)
    )
    for row in rows:
        found[row.incident_id].append(out(row))
    return found


def pending(followups: list[FollowUp]) -> bool:
    return any(f.state in PENDING_FOLLOWUP_STATES for f in followups)


def triggering_commit(state: MedicState) -> str | None:
    """The full SHA of the commit (or the deploy's head commit) that caused the incident."""
    rc = state.root_cause
    change = rc.triggering_change if rc else None
    if change is None or not change.ref or change.kind not in ("commit", "deploy"):
        return None
    for marker in state.changes:
        if change.kind == "commit" and marker.commit and marker.commit.startswith(change.ref):
            return marker.commit
        if change.kind == "deploy" and marker.ref == change.ref and marker.commit:
            return marker.commit
    return change.ref if change.kind == "commit" else None


async def create_on_resolve(
    session: AsyncSession, incident: Incident, state: MedicState, now: datetime
) -> list[IncidentFollowUp]:
    """One follow-up per applied fix PR that is not deployed yet (open, or merged with its deploy
    still to come). Idempotent per incident and PR. The caller records and commits."""
    existing = {
        (r.repo, r.number)
        for r in await session.scalars(
            select(IncidentFollowUp).where(IncidentFollowUp.incident_id == incident.id)
        )
    }
    sha = triggering_commit(state)
    created: list[IncidentFollowUp] = []
    for rec in state.fixes:
        pr, draft = rec.pull_request, rec.draft
        if rec.status != "applied" or pr is None or draft is None or pr.state == "closed":
            continue
        if (pr.repo, pr.number) in existing:
            continue
        row = IncidentFollowUp(
            incident_id=incident.id,
            workspace_id=incident.workspace_id,
            kind="fix_pr",
            state="merged" if pr.state == "merged" else "open",
            repo=pr.repo,
            number=pr.number,
            url=pr.url,
            base=draft.base,
            installation_id=draft.installation_id,
            triggering_commit=sha,
            merged_at=pr.merged_at,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        created.append(row)
    await session.flush()
    return created


async def record_change(
    session: AsyncSession, incident: Incident, run: Run, reason: str, now: datetime
) -> None:
    """An incident_updated step carrying every follow-up of the incident. The caller commits."""
    followups = (await for_incidents(session, [incident.id]))[incident.id]
    status: IncidentStatus = incident.status  # type: ignore[assignment]
    await record_step(
        session,
        run,
        kind="incident_updated",
        output_summary=f"{status}: {reason}",
        data=IncidentUpdated(
            incident_id=incident.id,
            status=status,
            previous_status=status,
            at=now,
            reason=reason,
            followups=followups,
        ),
    )


def remember_if_settled(incident: Incident, has_pending_pr: bool, now: datetime) -> None:
    """Incident memory may find the incident once no fix PR is still open (merged or closed)."""
    if incident.remembered_at is None and incident.status == "resolved" and not has_pending_pr:
        incident.remembered_at = now


# ------------------------------------------------------------------------------- tracking


async def _deploy_of(app: GitHubApp, row: IncidentFollowUp) -> dict[str, Any] | None:
    """A successful deploy workflow run on the base branch that contains the merge commit."""
    if not row.merge_commit or row.merged_at is None:
        return None
    repo = check_repo_name(row.repo)
    since = row.merged_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = await app.get(
        row.installation_id,
        f"/repos/{repo}/actions/runs",
        {
            "branch": row.base,
            "status": "success",
            "created": f">={since}",
            "per_page": MAX_DEPLOY_RUNS,
        },
    )
    for run in runs.get("workflow_runs", []):
        path = str(run.get("path") or run.get("name") or "").lower()
        head = str(run.get("head_sha") or "")
        if "deploy" not in path or run.get("conclusion") != "success" or not head:
            continue
        if head == row.merge_commit:
            return dict(run)
        compared = await app.get(
            row.installation_id, f"/repos/{repo}/compare/{row.merge_commit}...{head}"
        )
        if compared.get("status") in ("ahead", "identical"):
            return dict(run)
    return None


async def check_one(app: GitHubApp, row: IncidentFollowUp, now: datetime) -> str | None:
    """Advance one follow-up from GitHub. The new state's reason, or None when unchanged."""
    row.checked_at = now
    if row.state == "open":
        pr = await app.get(
            row.installation_id, f"/repos/{check_repo_name(row.repo)}/pulls/{row.number}"
        )
        if pr.get("merged"):
            row.state = "merged"
            row.merged_at = parse_time(pr.get("merged_at")) or now
            row.merge_commit = pr.get("merge_commit_sha") or None
            row.updated_at = now
            return f"Permanent fix: {row.repo}#{row.number} merged, waiting for its deploy"
        if pr.get("state") == "closed":
            row.state = "closed_unmerged"
            row.closed_at = parse_time(pr.get("closed_at")) or now
            row.updated_at = now
            return f"Permanent fix: {row.repo}#{row.number} was closed without merging"
        return None
    if row.state == "merged":
        if not row.merge_commit:
            # Merged before Medic resolved (or backfilled): learn the merge commit first.
            pr = await app.get(
                row.installation_id, f"/repos/{check_repo_name(row.repo)}/pulls/{row.number}"
            )
            row.merge_commit = pr.get("merge_commit_sha") or None
            row.merged_at = parse_time(pr.get("merged_at")) or row.merged_at
        run = await _deploy_of(app, row)
        if run is None:
            return None
        row.state = "deployed"
        row.deployed_at = parse_time(run.get("updated_at")) or now
        row.deploy_url = run.get("html_url") or None
        row.updated_at = now
        return f"Permanent fix: {row.repo}#{row.number} deployed"
    return None


def _sync_pull_request(incident: Incident, row: IncidentFollowUp) -> None:
    """Keep the fix's PullRequestRef in the incident state in step (memory reads it)."""
    state = MedicState.model_validate(incident.state or {})
    new = {"merged": "merged", "deployed": "merged", "closed_unmerged": "closed"}.get(row.state)
    if new is None:
        return
    for rec in state.fixes:
        pr = rec.pull_request
        if pr is not None and (pr.repo, pr.number) == (row.repo, row.number) and pr.state != new:
            rec.pull_request = pr.model_copy(update={"state": new, "merged_at": row.merged_at})
    dumped = state.model_dump(mode="json")
    if incident.state and "prs_checked_at" in incident.state:
        dumped["prs_checked_at"] = incident.state["prs_checked_at"]
    incident.state = dumped


async def track_followups(
    session_factory: SessionFactory, app: GitHubApp | None, *, now: datetime | None = None
) -> int:
    """Check the follow-ups that are due. Returns how many changed. Never raises for GitHub."""
    if app is None:
        return 0
    now = now or datetime.now(UTC)
    async with session_factory() as session:
        due = list(
            await session.scalars(
                select(IncidentFollowUp.id).where(
                    IncidentFollowUp.state.in_(PENDING_FOLLOWUP_STATES),
                    IncidentFollowUp.created_at > now - FOLLOWUP_TRACK_FOR,
                    (IncidentFollowUp.checked_at.is_(None))
                    | (IncidentFollowUp.checked_at <= now - FOLLOWUP_CHECK_EVERY),
                )
            )
        )
    changed = 0
    for followup_id in due:
        async with session_factory() as session:
            row = await session.get(IncidentFollowUp, followup_id, with_for_update=True)
            if row is None or row.state not in PENDING_FOLLOWUP_STATES:
                continue
            try:
                reason = await check_one(app, row, now)
            except GitHubError:
                logger.warning("follow-up check failed", extra={"followup": str(row.id)})
                row.checked_at = now
                await session.commit()
                continue
            if reason is not None:
                incident = await session.get(Incident, row.incident_id, with_for_update=True)
                run = (
                    await session.scalar(
                        select(Run)
                        .where(Run.incident_id == row.incident_id)
                        .order_by(Run.created_at.desc())
                        .limit(1)
                    )
                    if incident is not None
                    else None
                )
                if incident is not None:
                    _sync_pull_request(incident, row)
                    await session.flush()
                    still_open = await session.scalar(
                        select(IncidentFollowUp.id).where(
                            IncidentFollowUp.incident_id == incident.id,
                            IncidentFollowUp.state == "open",
                        )
                    )
                    remember_if_settled(incident, still_open is not None, now)
                    if run is not None:
                        await record_change(session, incident, run, reason, now)
                changed += 1
            await session.commit()
    return changed
