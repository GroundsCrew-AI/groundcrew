"""Incident and approval notifications, per workspace: Slack (an incoming webhook) and email.

Four events: an incident opened (not a signal deduped into an open incident, never a test
incident), fixes waiting for approval, an incident resolved, an incident failed. The owner picks
the events, the email audience (members, owners, or nobody), and a Slack webhook
(app.workspace_settings).

The event's own transaction enqueues one `notify` job per channel and recipient
(`enqueue_event`), so a send that fails or hangs never touches Medic or the watcher; the job
retries with the queue's backoff (MAX_ATTEMPTS). Every job of an event carries the same
deterministic key (event, incident, approvals), and an event whose key is already queued or
sent is not enqueued again. The text is built when the job runs, from the incident as it is
then.

Messages link to the incident page and never to approve or reject: link scanners (mail
security gateways, Slack unfurls) open every link they see, and approving must stay an
authenticated action in the app, with its single-use token. People approve on the incident's
approval card.

The webhook URL is a secret: stored encrypted, never returned by the API, never in an error
message, and masked in logs (app.security.redact).
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import httpx
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import queue
from app.config import Settings, get_settings
from app.db.models import (
    Approval,
    ApprovalStatus,
    Incident,
    Job,
    Resource,
    User,
    Workspace,
    WorkspaceMember,
    WorkspaceRole,
)
from app.db.session import SessionFactory
from app.mail import Mail, Mailer, MailError, mailer_for
from app.security.redact import summarize
from app.workspace_settings import get_config, slack_saved, slack_webhook

log = logging.getLogger(__name__)

NOTIFY_JOB: Final = "notify"
MAX_ATTEMPTS = 5
SLACK_TIMEOUT_SECONDS = 10.0

Event = Literal["incident_opened", "approval_needed", "incident_resolved", "incident_failed"]
Channel = Literal["slack", "email"]

_HEADLINE: dict[str, str] = {
    "incident_opened": "Incident opened",
    "approval_needed": "Approval needed",
    "incident_resolved": "Incident resolved",
    "incident_failed": "Incident failed",
    "test": "Test notification",
}

# Tests swap in httpx.MockTransport; None is the network.
slack_transport: httpx.AsyncBaseTransport | None = None


class NotifyError(Exception):
    """A channel did not take the message. Never includes the webhook URL."""


@dataclass(frozen=True)
class FixNotice:
    title: str
    # "what: before → after", or the pull request a PR fix opens.
    lines: list[str]


@dataclass(frozen=True)
class Notice:
    event: Event | Literal["test"]
    title: str
    severity: str
    resources: list[str]
    summary: str
    link: str
    fixes: list[FixNotice] = field(default_factory=list)
    # Why it failed, for incident_failed.
    reason: str | None = None

    @property
    def headline(self) -> str:
        return _HEADLINE[self.event]


# ------------------------------------------------------------------------------------- enqueue


def event_key(event: Event, incident_id: uuid.UUID, approval_ids: Sequence[uuid.UUID] = ()) -> str:
    key = f"{event}:{incident_id}"
    if approval_ids:
        key += ":" + ",".join(sorted(str(a) for a in approval_ids))
    return key


async def email_recipients(
    session: AsyncSession, workspace_id: uuid.UUID, audience: str
) -> list[User]:
    """Members (or owners) with a confirmed email address."""
    if audience not in ("members", "owners"):
        return []
    query = (
        select(User)
        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .where(
            WorkspaceMember.workspace_id == workspace_id,
            User.email.is_not(None),
            User.email_verified_at.is_not(None),
        )
        .order_by(WorkspaceMember.created_at)
    )
    if audience == "owners":
        query = query.where(WorkspaceMember.role == WorkspaceRole.owner)
    return list(await session.scalars(query))


async def enqueue_event(
    session: AsyncSession,
    incident: Incident,
    event: Event,
    *,
    approval_ids: Sequence[uuid.UUID] = (),
) -> int:
    """Enqueue this event's notifications in the caller's transaction (the caller commits).
    Returns how many jobs were added. Never raises: a notification problem must not stop an
    incident from opening or Medic from going on, so everything happens in a savepoint that is
    rolled back on error."""
    if incident.is_test:
        return 0
    try:
        async with session.begin_nested():
            return await _enqueue(session, incident, event, approval_ids)
    except Exception:
        log.exception("notifications for %s on incident %s were not enqueued", event, incident.id)
        return 0


async def _enqueue(
    session: AsyncSession, incident: Incident, event: Event, approval_ids: Sequence[uuid.UUID]
) -> int:
    workspace = await session.get(Workspace, incident.workspace_id)
    if workspace is None:
        return 0
    try:
        config = get_config(workspace).notifications
    except ValidationError:
        log.warning("workspace %s has unreadable settings; no notifications", workspace.id)
        return 0
    if not getattr(config, event):
        return 0
    targets: list[dict[str, Any]] = []
    if slack_saved(workspace):
        targets.append({"channel": "slack"})
    for user in await email_recipients(session, workspace.id, config.email):
        targets.append({"channel": "email", "user_id": str(user.id)})
    if not targets:
        return 0
    key = event_key(event, incident.id, approval_ids)
    # Two transactions with the same event (a retried job, two workers) wait here, and the
    # second one sees the first one's jobs.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"notify:{key}"}
    )
    seen = await session.scalar(
        select(Job.id)
        .where(Job.kind == NOTIFY_JOB, Job.payload["event_key"].astext == key)
        .limit(1)
    )
    if seen is not None:
        return 0
    for target in targets:
        await queue.enqueue(
            session,
            NOTIFY_JOB,
            {
                "event_key": key,
                "event": event,
                "incident_id": str(incident.id),
                "approval_ids": [str(a) for a in approval_ids],
                **target,
            },
            max_attempts=MAX_ATTEMPTS,
        )
    return len(targets)


# -------------------------------------------------------------------------------------- notice


def incident_link(settings: Settings, incident_id: uuid.UUID | str) -> str:
    return f"{settings.web_origin.rstrip('/')}/incidents/{incident_id}"


def _one_line(value: object, limit: int = 300) -> str:
    line = " ".join(str(value or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _fix_notice(record: dict[str, Any]) -> FixNotice:
    fix = record.get("fix") or {}
    lines = [
        f"{p.get('what')}: {p.get('before') or '(none)'} → {p.get('after') or '(none)'}"
        for p in fix.get("plan_lines") or []
        if isinstance(p, dict)
    ]
    pr = fix.get("pull_request")
    if isinstance(pr, dict) and pr.get("repo"):
        lines.append(f"Pull request on {pr['repo']}: {pr.get('title') or fix.get('title')}")
    return FixNotice(
        title=_one_line(fix.get("title") or "Fix"), lines=[_one_line(x) for x in lines]
    )


async def build_notice(
    session: AsyncSession,
    settings: Settings,
    incident: Incident,
    event: Event,
    approval_ids: Sequence[uuid.UUID] = (),
) -> Notice | None:
    """The message for `event`, or None when there is nothing left to say (every approval of
    an approval_needed event was decided before the job ran)."""
    state: dict[str, Any] = incident.state if isinstance(incident.state, dict) else {}
    fixes: list[FixNotice] = []
    if event == "approval_needed":
        pending = set(
            await session.scalars(
                select(Approval.id).where(
                    Approval.id.in_(list(approval_ids)),
                    Approval.status == ApprovalStatus.pending,
                )
            )
        )
        if not pending:
            return None
        fixes = [
            _fix_notice(rec)
            for rec in state.get("fixes") or []
            if isinstance(rec, dict) and str(rec.get("approval_id")) in {str(p) for p in pending}
        ]
    names = list(
        await session.scalars(
            select(Resource.name).where(Resource.id.in_(list(incident.affected_resource_ids)))
        )
    )
    root_cause = state.get("root_cause") if isinstance(state.get("root_cause"), dict) else {}
    signals = incident.signals or []
    summary = (
        (root_cause or {}).get("summary")
        or incident.summary
        or (signals[-1].get("detail") if signals and isinstance(signals[-1], dict) else None)
        or incident.title
    )
    outcome = state.get("outcome") if isinstance(state.get("outcome"), dict) else {}
    reason = None
    if event == "incident_failed":
        reason = (outcome or {}).get("reason") or state.get("error")
    return Notice(
        event=event,
        title=_one_line(incident.title, 200),
        severity=incident.severity or "unknown",
        resources=names,
        summary=_one_line(summary),
        link=incident_link(settings, incident.id),
        fixes=fixes,
        reason=_one_line(reason) if reason else None,
    )


def sample_notice(settings: Settings, workspace: Workspace) -> Notice:
    return Notice(
        event="test",
        title=f"Notifications for {workspace.name} work",
        severity="low",
        resources=["example-service"],
        summary="A test from the Settings page. Incidents and approvals look like this.",
        link=f"{settings.web_origin.rstrip('/')}/incidents",
    )


# ---------------------------------------------------------------------------------- formatting


def _slack_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_payload(notice: Notice) -> dict[str, Any]:
    """Block Kit: header, severity and resources, summary, the fixes (approval_needed), and an
    "Open incident" link button. No approve/reject buttons (see the module docstring)."""
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{notice.headline}: {notice.title}"[:150]},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Severity*\n{_slack_escape(notice.severity)}"},
                {
                    "type": "mrkdwn",
                    "text": "*Affected*\n"
                    + _slack_escape(", ".join(notice.resources[:10]) or "(none)")[:1900],
                },
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": _slack_escape(notice.summary)}},
    ]
    if notice.reason:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Why it failed*\n{_slack_escape(notice.reason)}",
                },
            }
        )
    for fix in notice.fixes[:10]:
        body = "\n".join(f"• {_slack_escape(line)}" for line in fix.lines[:10])
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{_slack_escape(fix.title)}*" + (f"\n{body}" if body else ""),
                },
            }
        )
    if notice.fixes:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Approve or reject on the incident page.",
                    }
                ],
            }
        )
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open incident"},
                    "url": notice.link,
                    **({"style": "primary"} if notice.event == "approval_needed" else {}),
                }
            ],
        }
    )
    return {"text": f"{notice.headline}: {notice.title}", "blocks": blocks}


def email_mail(notice: Notice, to: str) -> Mail:
    lines = [
        f"{notice.headline}: {notice.title}",
        "",
        f"Severity: {notice.severity}",
        f"Affected: {', '.join(notice.resources) or '(none)'}",
        f"Summary: {notice.summary}",
    ]
    if notice.reason:
        lines.append(f"Why it failed: {notice.reason}")
    if notice.fixes:
        lines += ["", "Waiting for approval:"]
        for fix in notice.fixes:
            lines.append(f"- {fix.title}")
            lines += [f"    {line}" for line in fix.lines]
        lines += [
            "",
            "Approve or reject on the incident page (sign in to Groundcrew; approvals are",
            "never made from email):",
        ]
    else:
        lines += ["", "Open the incident:"]
    lines += [notice.link, ""]
    return Mail(
        to=to,
        subject=f"[{notice.severity}] {notice.headline}: {notice.title}"[:200],
        text="\n".join(lines),
    )


# -------------------------------------------------------------------------------------- sending


async def send_slack(url: str, payload: dict[str, Any]) -> None:
    """POST to the webhook. NotifyError says what went wrong without the URL."""
    try:
        async with httpx.AsyncClient(
            timeout=SLACK_TIMEOUT_SECONDS, transport=slack_transport, follow_redirects=False
        ) as client:
            response = await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        raise NotifyError(f"Slack did not answer ({type(exc).__name__}).") from None
    if response.status_code != 200:
        body = summarize(response.text, 100) or ""
        raise NotifyError(f"Slack refused the message: HTTP {response.status_code} {body}".strip())


async def send_email(mailer: Mailer, notice: Notice, to: str) -> None:
    try:
        await mailer.send(email_mail(notice, to))
    except MailError as exc:
        raise NotifyError(str(exc)) from None


def job_mailer(settings: Settings) -> Mailer:
    """The worker's mailer (tests replace it)."""
    return mailer_for(settings)


async def notify_job(session_factory: SessionFactory, payload: dict[str, Any]) -> None:
    """Send one notification. Raises NotifyError when the channel fails, so the worker retries
    (MAX_ATTEMPTS); anything that went away meanwhile (the incident, the webhook, the member)
    ends the job quietly."""
    settings = get_settings()
    event: Event = payload["event"]
    channel: Channel = payload["channel"]
    approval_ids = [uuid.UUID(a) for a in payload.get("approval_ids") or []]
    async with session_factory() as session:
        incident = await session.get(Incident, uuid.UUID(payload["incident_id"]))
        workspace = await session.get(Workspace, incident.workspace_id) if incident else None
        if incident is None or workspace is None:
            return
        notice = await build_notice(session, settings, incident, event, approval_ids)
        if notice is None:
            return
        url: str | None = None
        to: str | None = None
        if channel == "slack":
            url = slack_webhook(workspace)
        else:
            user_id = uuid.UUID(payload["user_id"])
            member = await session.scalar(
                select(User)
                .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
                .where(WorkspaceMember.workspace_id == workspace.id, User.id == user_id)
            )
            if member is not None and member.email_verified_at is not None:
                to = member.email
    # Sent outside the session: a slow Slack or SMTP server holds no connection.
    if url is not None:
        await send_slack(url, slack_payload(notice))
        log.info("notification sent", extra={"event": event, "channel": "slack"})
    elif to is not None:
        await send_email(job_mailer(settings), notice, to)
        log.info("notification sent", extra={"event": event, "channel": "email"})


@dataclass(frozen=True)
class ChannelResult:
    channel: Channel
    ok: bool
    detail: str


async def send_test(
    session: AsyncSession, settings: Settings, workspace: Workspace, mailer: Mailer
) -> list[ChannelResult]:
    """Send a test notice to every channel set up for the workspace, now. One result per
    channel; an empty list when none is set up."""
    notice = sample_notice(settings, workspace)
    config = get_config(workspace).notifications
    results: list[ChannelResult] = []
    if slack_saved(workspace):
        url = slack_webhook(workspace)
        if url is None:
            results.append(
                ChannelResult("slack", False, "The saved webhook cannot be read. Save it again.")
            )
        else:
            try:
                await send_slack(url, slack_payload(notice))
                results.append(ChannelResult("slack", True, "Sent to the Slack channel."))
            except NotifyError as exc:
                results.append(ChannelResult("slack", False, str(exc)))
    if config.email != "none":
        people = await email_recipients(session, workspace.id, config.email)
        if not people:
            results.append(
                ChannelResult("email", False, "Nobody in the audience has a confirmed email.")
            )
        else:
            failed = 0
            for user in people:
                try:
                    await send_email(mailer, notice, user.email or "")
                except NotifyError:
                    failed += 1
            sent = len(people) - failed
            results.append(
                ChannelResult(
                    "email",
                    failed == 0,
                    f"Sent to {sent} of {len(people)} people."
                    if failed
                    else f"Sent to {sent} {'person' if sent == 1 else 'people'}.",
                )
            )
    return results
