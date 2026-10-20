"""Incident and approval notifications: which events enqueue jobs (and which never do), the
owner's toggles and audience, the Slack and email messages, the write-only webhook, the test
endpoint, retries, and dedupe. Slack is an httpx.MockTransport; email is a MemoryMailer."""

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select

from app import notifications
from app.agents.medic.events import Signal
from app.agents.medic.jobs import investigate_incident_job
from app.config import get_settings
from app.db.models import (
    Approval,
    ApprovalStatus,
    Incident,
    Job,
    JobStatus,
    Resource,
    Run,
    RunStatus,
    ToolPolicy,
    User,
    Workspace,
)
from app.db.session import SessionFactory, get_session_factory
from app.incidents.store import open_incident, set_status
from app.llm.fake import FakeLLM, reply
from app.mail import Mail, MailError, MemoryMailer, get_mailer
from app.main import app
from app.pipeline.resume import resume_run_job
from app.policy.engine import Decision
from app.security.crypto import generate_key
from app.security.redact import redact
from app.worker import Worker
from app.workspace_settings import (
    SLACK_WEBHOOK_KEY,
    NotificationConfig,
    get_config,
    set_config,
    set_slack_webhook,
)
from tests.test_api import Member, _member, _settings
from tests.test_medic import (
    BAD_SHA,
    BAD_TASK,
    Harness,
    _decide,
    _state,
    demo_graph,
    github_routes,
    oom_evidence,
    oom_fix,
    oom_root_cause,
    settings,
    use_deps,  # noqa: F401 (fixture)
)

pytestmark = pytest.mark.anyio

HOOK = "https://hooks.slack.com/services/T0123ABC/B0456DEF/abcdefSECRETtoken123456"


@pytest.fixture(autouse=True)
def secrets_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SECRETS_KEY", generate_key())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class Slack:
    """A fake incoming webhook: records each payload, answers `status`."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.posts: list[dict[str, Any]] = []
        self.urls: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.urls.append(str(request.url))
            self.posts.append(json.loads(request.content))
            return httpx.Response(self.status, text="ok" if self.status == 200 else "no_service")

        return httpx.MockTransport(handler)


@pytest.fixture
def slack(monkeypatch: pytest.MonkeyPatch) -> Slack:
    fake = Slack()
    monkeypatch.setattr(notifications, "slack_transport", fake.transport())
    return fake


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch) -> MemoryMailer:
    mailer = MemoryMailer()
    monkeypatch.setattr(notifications, "job_mailer", lambda _settings: mailer)
    return mailer


async def _configure(
    sf: SessionFactory,
    ws: uuid.UUID,
    *,
    hook: str | None = HOOK,
    verified: bool = True,
    **config: Any,
) -> None:
    async with sf() as session:
        workspace = await session.get(Workspace, ws)
        assert workspace is not None
        current = get_config(workspace)
        set_config(
            workspace,
            current.model_copy(update={"notifications": NotificationConfig(**config)}),
        )
        set_slack_webhook(workspace, hook)
        owner = await session.get(User, workspace.owner_id)
        assert owner is not None
        owner.email_verified_at = datetime.now(UTC) if verified else None
        await session.commit()


async def _resource(sf: SessionFactory, ws: uuid.UUID, name: str = "checkout-api") -> uuid.UUID:
    async with sf() as session:
        r = Resource(
            workspace_id=ws,
            provider="aws",
            type="ecs:service",
            name=name,
            external_id=f"ecs:{name}",
        )
        session.add(r)
        await session.commit()
        return r.id


async def _open(
    sf: SessionFactory, ws: uuid.UUID, rid: uuid.UUID, *, is_test: bool = False, detail: str = "x"
) -> tuple[uuid.UUID, bool]:
    async with sf() as session:
        opened = await open_incident(
            session,
            workspace_id=ws,
            resource_ids=[rid],
            detected_by="watcher",
            signals=[
                Signal(
                    rule="health_check",
                    resource_id=rid,
                    resource_name="checkout-api",
                    detail=detail,
                    at=datetime.now(UTC) - timedelta(minutes=1),
                )
            ],
            is_test=is_test,
        )
        return opened.incident.id, opened.deduped


async def _jobs(sf: SessionFactory) -> list[dict[str, Any]]:
    async with sf() as session:
        jobs = await session.scalars(
            select(Job).where(Job.kind == notifications.NOTIFY_JOB).order_by(Job.created_at)
        )
        return [dict(j.payload) for j in jobs]


async def _run_notify_jobs(sf: SessionFactory) -> None:
    for payload in await _jobs(sf):
        await notifications.notify_job(sf, payload)


# --- which events enqueue ------------------------------------------------------------------------


async def test_opening_enqueues_slack_and_email_but_not_dedupes_or_tests(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    sf = session_factory
    await _configure(sf, workspace.id)
    rid = await _resource(sf, workspace.id)
    incident_id, deduped = await _open(sf, workspace.id, rid)
    assert not deduped
    jobs = await _jobs(sf)
    assert sorted(j["channel"] for j in jobs) == ["email", "slack"]
    assert {j["event_key"] for j in jobs} == {f"incident_opened:{incident_id}"}
    assert all("run_id" not in j for j in jobs)  # a give-up must not fail the Medic run

    # The same signal again joins the open incident: no new notification.
    _, deduped = await _open(sf, workspace.id, rid, detail="again")
    assert deduped and len(await _jobs(sf)) == 2
    # A test incident never notifies.
    await _open(sf, workspace.id, await _resource(sf, workspace.id, "other"), is_test=True)
    assert len(await _jobs(sf)) == 2


async def test_toggles_and_audience_are_respected(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    sf = session_factory
    await _configure(sf, workspace.id, incident_opened=False)
    rid = await _resource(sf, workspace.id)
    incident_id, _ = await _open(sf, workspace.id, rid)
    assert await _jobs(sf) == []

    # Resolved is on; email to nobody and no Slack: nothing to send, so no job either.
    await _configure(sf, workspace.id, hook=None, email="none")
    async with sf() as session:
        incident = await session.get(Incident, incident_id)
        run = await session.scalar(select(Run).where(Run.incident_id == incident_id))
        assert incident is not None and run is not None
        await set_status(session, incident, run, "resolved")
        await session.commit()
    assert await _jobs(sf) == []

    # An unconfirmed address never gets mail.
    await _configure(sf, workspace.id, hook=None, verified=False)
    async with sf() as session:
        incident = await session.get(Incident, incident_id)
        run = await session.scalar(select(Run).where(Run.incident_id == incident_id))
        assert incident is not None and run is not None
        await set_status(session, incident, run, "failed")
        await session.commit()
    assert await _jobs(sf) == []


async def test_resolved_and_failed_enqueue_once(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    sf = session_factory
    await _configure(sf, workspace.id, hook=None)
    rid = await _resource(sf, workspace.id)
    incident_id, _ = await _open(sf, workspace.id, rid)
    async with sf() as session:
        incident = await session.get(Incident, incident_id)
        run = await session.scalar(select(Run).where(Run.incident_id == incident_id))
        assert incident is not None and run is not None
        await set_status(session, incident, run, "resolved")
        await session.commit()
        # Dedupe: the same event and incident again is not enqueued twice.
        assert await notifications.enqueue_event(session, incident, "incident_resolved") == 0
        await session.commit()
    events = [j["event"] for j in await _jobs(sf)]
    assert events == ["incident_opened", "incident_resolved"]


async def test_medic_approval_needed_and_failed(
    session_factory: SessionFactory,
    workspace: Workspace,
    use_deps: Any,  # noqa: F811
    slack: Slack,
) -> None:
    sf = session_factory
    await _configure(sf, workspace.id, email="none")
    ids = await demo_graph(sf, workspace.id)
    ev = oom_evidence(ids, datetime.now(UTC).replace(microsecond=0))
    llm = FakeLLM(reply(oom_root_cause(ev)), reply(oom_fix()), settings=settings())
    gh = github_routes(BAD_SHA, {"worker/ecs-task.json": ("+x", BAD_TASK)})
    h = Harness(sf, ev, llm, actions=None, gh=gh)
    use_deps(h)
    incident_id, _ = await _open(sf, workspace.id, ids["worker"])
    async with sf() as session:
        run = await session.scalar(select(Run).where(Run.incident_id == incident_id))
        assert run is not None
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run.id)})
    _, state, _ = await _state(sf, incident_id)
    approval_id = state.fixes[0].approval_id
    jobs = await _jobs(sf)
    assert [j["event"] for j in jobs] == ["incident_opened", "approval_needed"]
    assert jobs[1]["approval_ids"] == [str(approval_id)]

    # The approval message: the fix and a link to the incident page, never an approve link.
    await notifications.notify_job(sf, jobs[1])
    blocks = slack.posts[-1]["blocks"]
    assert blocks[0]["text"]["text"].startswith("Approval needed: ")
    assert any(state.fixes[0].fix.title in json.dumps(b) for b in blocks)
    buttons = [e for b in blocks if b["type"] == "actions" for e in b["elements"]]
    assert [b["url"] for b in buttons] == [f"http://localhost:3000/incidents/{incident_id}"]
    assert "approve" not in json.dumps(buttons).lower()

    await _decide(sf, workspace, approval_id, Decision.reject)
    await resume_run_job(sf, {"run_id": str(run.id), "approval_id": "x"})
    incident, _, _ = await _state(sf, incident_id)
    assert incident.status == "failed"
    assert [j["event"] for j in await _jobs(sf)][-1] == "incident_failed"
    # The approval was decided: its (late) message is skipped.
    posts = len(slack.posts)
    await notifications.notify_job(sf, jobs[1])
    assert len(slack.posts) == posts


# --- messages -------------------------------------------------------------------------------------


async def _approval_incident(sf: SessionFactory, ws: Workspace) -> tuple[Incident, uuid.UUID]:
    rid = await _resource(sf, ws.id)
    async with sf() as session:
        run = Run(workspace_id=ws.id, agent="medic", goal="g", status=RunStatus.waiting_approval)
        session.add(run)
        await session.flush()
        approval = Approval(
            run_id=run.id,
            workspace_id=ws.id,
            tool_call_id="medic:x:fix-1",
            tool_name="rollback_ecs_service",
            policy=ToolPolicy.emergency,
            arguments={},
            status=ApprovalStatus.pending,
        )
        session.add(approval)
        await session.flush()
        incident = Incident(
            workspace_id=ws.id,
            title="checkout-api: health check failing",
            severity="critical",
            status="waiting_approval",
            affected_resource_ids=[rid],
            signals=[{"rule": "health_check", "detail": "GET /health 503"}],
            state={
                "root_cause": {"summary": "Task definition 42 has 24 MB of memory."},
                "fixes": [
                    {
                        "approval_id": str(approval.id),
                        "status": "waiting_approval",
                        "fix": {
                            "title": "Roll back checkout-api to revision 41",
                            "plan_lines": [
                                {"what": "task definition", "before": "api:42", "after": "api:41"}
                            ],
                        },
                    }
                ],
            },
        )
        session.add(incident)
        await session.commit()
        return incident, approval.id


async def test_slack_payload_and_email_content(
    session_factory: SessionFactory, workspace: Workspace, slack: Slack, outbox: MemoryMailer
) -> None:
    sf = session_factory
    await _configure(sf, workspace.id)
    incident, approval_id = await _approval_incident(sf, workspace)
    async with sf() as session:
        added = await notifications.enqueue_event(
            session, incident, "approval_needed", approval_ids=[approval_id]
        )
        await session.commit()
    assert added == 2
    await _run_notify_jobs(sf)

    assert slack.urls == [HOOK]
    payload = slack.posts[0]
    link = f"http://localhost:3000/incidents/{incident.id}"
    assert payload["text"] == "Approval needed: checkout-api: health check failing"
    types = [b["type"] for b in payload["blocks"]]
    assert types == ["header", "section", "section", "section", "context", "actions"]
    fields = payload["blocks"][1]["fields"]
    assert fields[0]["text"] == "*Severity*\ncritical" and "checkout-api" in fields[1]["text"]
    assert payload["blocks"][2]["text"]["text"] == "Task definition 42 has 24 MB of memory."
    fix = payload["blocks"][3]["text"]["text"]
    assert fix == "*Roll back checkout-api to revision 41*\n• task definition: api:42 → api:41"
    button = payload["blocks"][-1]["elements"][0]
    assert button["text"]["text"] == "Open incident" and button["url"] == link

    [mail] = outbox.outbox
    assert mail.to == "test@groundcrew.local"
    assert mail.subject == "[critical] Approval needed: checkout-api: health check failing"
    assert "Affected: checkout-api" in mail.text
    assert "Summary: Task definition 42 has 24 MB of memory." in mail.text
    assert "- Roll back checkout-api to revision 41" in mail.text
    assert "task definition: api:42 → api:41" in mail.text
    assert link in mail.text and "approvals are\nnever made from email" in mail.text


# --- delivery ------------------------------------------------------------------------------------


async def test_a_failed_send_retries_and_never_reaches_medic(
    session_factory: SessionFactory,
    workspace: Workspace,
    slack: Slack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sf = session_factory
    await _configure(sf, workspace.id, email="none")
    slack.status = 500
    rid = await _resource(sf, workspace.id)
    await _open(sf, workspace.id, rid)
    async with sf() as session:  # only the notification: no Medic in this test
        await session.execute(delete(Job).where(Job.kind != notifications.NOTIFY_JOB))
        await session.commit()

    worker = Worker(sf, {notifications.NOTIFY_JOB: notifications.notify_job}, worker_id="w")
    assert await worker.run_once()
    async with sf() as session:
        job = await session.scalar(select(Job).where(Job.kind == notifications.NOTIFY_JOB))
        assert job is not None
        assert job.status == JobStatus.queued and job.attempts == 1 and job.max_attempts == 5
        assert job.last_error is not None and "HTTP 500" in job.last_error
        assert "SECRETtoken" not in job.last_error

    # Enqueueing never raises into the incident's transaction, whatever goes wrong.
    async def broken(*_: Any) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(notifications, "_enqueue", broken)
    other = await _resource(sf, workspace.id, "lonely")
    incident_id, _ = await _open(sf, workspace.id, other)
    async with sf() as session:
        assert await session.get(Incident, incident_id) is not None


async def test_email_failure_raises_for_retry(
    session_factory: SessionFactory, workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Down:
        async def send(self, mail: Mail) -> None:
            raise MailError("Could not send email: ConnectionRefusedError")

    monkeypatch.setattr(notifications, "job_mailer", lambda _settings: Down())
    sf = session_factory
    await _configure(sf, workspace.id, hook=None)
    await _open(sf, workspace.id, await _resource(sf, workspace.id))
    [payload] = await _jobs(sf)
    with pytest.raises(notifications.NotifyError):
        await notifications.notify_job(sf, payload)


def test_webhooks_are_redacted_in_logs() -> None:
    assert "SECRETtoken" not in redact(f"posting to {HOOK} failed")


# --- API -----------------------------------------------------------------------------------------


@pytest.fixture
def mailer() -> MemoryMailer:
    return MemoryMailer()


@pytest.fixture
async def client(
    session_factory: SessionFactory, mailer: MemoryMailer
) -> AsyncIterator[httpx.AsyncClient]:
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = lambda: _settings()
    app.dependency_overrides[get_mailer] = lambda: mailer
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c
    app.dependency_overrides.clear()


async def _verified(sf: SessionFactory, member: Member, email: str) -> None:
    async with sf() as session:
        user = await session.get(User, member.user_id)
        assert user is not None
        user.email, user.email_verified_at = email, datetime.now(UTC)
        await session.commit()


async def test_webhook_is_encrypted_write_only_and_owner_only(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    owner = await _member(session_factory, 8101, "nora")
    ws = owner.workspace_id
    url = f"/workspaces/{ws}"
    detail = (await client.get(url, headers=owner.headers)).json()
    assert detail["slack_saved"] is False
    assert detail["settings"]["notifications"] == {
        "email": "owners",
        "incident_opened": True,
        "approval_needed": True,
        "incident_resolved": True,
        "incident_failed": True,
    }

    bad = await client.patch(
        url, json={"slack_webhook_url": "https://evil.example/services/x"}, headers=owner.headers
    )
    assert bad.status_code == 422 and "hooks.slack.com" in bad.json()["detail"]
    r = await client.patch(url, json={"slack_webhook_url": HOOK}, headers=owner.headers)
    assert r.status_code == 200 and r.json()["slack_saved"] is True
    assert "SECRETtoken" not in r.text
    assert "SECRETtoken" not in (await client.get(url, headers=owner.headers)).text
    async with session_factory() as session:
        stored = (await session.get(Workspace, ws)).settings  # type: ignore[union-attr]
        assert stored[SLACK_WEBHOOK_KEY] and "SECRETtoken" not in json.dumps(stored)

    # Saving other settings (an older client: no `notifications`) keeps both.
    await client.patch(
        url,
        json={"settings": {"notifications": {"email": "members", "incident_resolved": False}}},
        headers=owner.headers,
    )
    r = await client.patch(url, json={"settings": {"incident_watch": False}}, headers=owner.headers)
    body = r.json()
    assert body["slack_saved"] is True and body["settings"]["incident_watch"] is False
    assert body["settings"]["notifications"]["email"] == "members"
    assert body["settings"]["notifications"]["incident_resolved"] is False

    # Members read, but cannot change or test anything.
    sam = await _member(session_factory, 8102, "samir")
    added = await client.post(
        f"{url}/members", json={"github_login": "samir"}, headers=owner.headers
    )
    assert added.status_code == 200
    assert (await client.get(url, headers=sam.headers)).json()["slack_saved"] is True
    for call in (
        client.patch(url, json={"slack_webhook_url": None}, headers=sam.headers),
        client.patch(
            url, json={"settings": {"notifications": {"email": "none"}}}, headers=sam.headers
        ),
        client.post(f"{url}/notifications/test", headers=sam.headers),
    ):
        assert (await call).status_code == 403

    # null clears it.
    r = await client.patch(url, json={"slack_webhook_url": None}, headers=owner.headers)
    assert r.json()["slack_saved"] is False


async def test_test_endpoint_reports_each_channel(
    client: httpx.AsyncClient,
    session_factory: SessionFactory,
    mailer: MemoryMailer,
    slack: Slack,
) -> None:
    owner = await _member(session_factory, 8201, "omar")
    ws = owner.workspace_id
    test = f"/workspaces/{ws}/notifications/test"
    await client.patch(
        f"/workspaces/{ws}", json={"settings": {"notifications": {"email": "none"}}},
        headers=owner.headers,
    )  # fmt: skip
    assert (await client.post(test, headers=owner.headers)).json() == {"results": []}

    await _verified(session_factory, owner, "omar@example.com")
    await client.patch(
        f"/workspaces/{ws}",
        json={"slack_webhook_url": HOOK, "settings": {"notifications": {"email": "owners"}}},
        headers=owner.headers,
    )
    r = await client.post(test, headers=owner.headers)
    assert r.status_code == 200
    assert r.json()["results"] == [
        {"channel": "slack", "ok": True, "detail": "Sent to the Slack channel."},
        {"channel": "email", "ok": True, "detail": "Sent to 1 person."},
    ]
    assert slack.posts[0]["blocks"][0]["text"]["text"].startswith("Test notification: ")
    assert [m.to for m in mailer.outbox] == ["omar@example.com"]

    slack.status = 404
    r = await client.post(test, headers=owner.headers)
    first = r.json()["results"][0]
    assert first["ok"] is False and "HTTP 404" in first["detail"] and "SECRET" not in r.text
