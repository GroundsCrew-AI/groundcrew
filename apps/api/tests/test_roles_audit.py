"""Workspace roles (owner, approver, viewer) and the audit log.

Viewers read everything in their workspace and change nothing (403). Approvers also start work
and decide approvals. Owners also manage members, connections, and settings. Every decision and
every change to members, connections, and settings is in the audit log, which members read.
"""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.models import (
    Approval,
    ApprovalStatus,
    AuditEvent,
    Run,
    RunStatus,
    ToolPolicy,
    Workspace,
)
from app.db.session import SessionFactory, get_session_factory
from app.main import app
from app.workspace_settings import WorkspaceConfig

from .test_api import Member, _member, _settings

pytestmark = pytest.mark.anyio


@pytest.fixture
async def client(session_factory: SessionFactory) -> AsyncIterator[httpx.AsyncClient]:
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = lambda: _settings()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def owner(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 8001, "olivia")


async def _add(
    client: httpx.AsyncClient, sf: SessionFactory, owner: Member, gid: int, login: str, role: str
) -> Member:
    person = await _member(sf, gid, login)
    r = await client.post(
        f"/workspaces/{owner.workspace_id}/members",
        json={"github_login": login, "role": role},
        headers={**owner.headers, "x-client-ip": "203.0.113.7"},
    )
    assert r.status_code == 200, r.text
    return Member(person.user_id, owner.workspace_id, person.headers)


async def _pending(sf: SessionFactory, workspace_id: uuid.UUID, policy: ToolPolicy) -> uuid.UUID:
    async with sf() as session:
        run = Run(
            workspace_id=workspace_id, agent="deploy", goal="g", status=RunStatus.waiting_approval
        )
        session.add(run)
        await session.flush()
        approval = Approval(
            run_id=run.id,
            workspace_id=workspace_id,
            tool_call_id=f"c-{uuid.uuid4().hex[:6]}",
            tool_name="target_apply",
            policy=policy,
            arguments={"plan_hash": "a" * 64},
            status=ApprovalStatus.pending,
        )
        session.add(approval)
        await session.commit()
        return approval.id


async def test_new_members_are_viewers_unless_a_role_is_given(
    client: httpx.AsyncClient, session_factory: SessionFactory, owner: Member
) -> None:
    await _member(session_factory, 8002, "vic")
    r = await client.post(
        f"/workspaces/{owner.workspace_id}/members",
        json={"github_login": "vic"},
        headers=owner.headers,
    )
    roles = {m["github_login"]: m["role"] for m in r.json()["members"]}
    assert roles == {"olivia": "owner", "vic": "viewer"}


async def test_viewers_read_but_change_nothing(
    client: httpx.AsyncClient, session_factory: SessionFactory, owner: Member
) -> None:
    ws = owner.workspace_id
    viewer = await _add(client, session_factory, owner, 8003, "vera", "viewer")
    approval_id = await _pending(session_factory, ws, ToolPolicy.write)

    for path in (
        f"/workspaces/{ws}",
        f"/workspaces/{ws}/runs",
        f"/workspaces/{ws}/approvals",
        f"/workspaces/{ws}/members",
        f"/workspaces/{ws}/audit",
        f"/workspaces/{ws}/graph",
        f"/incidents?workspace_id={ws}",
        f"/deploys?workspace_id={ws}",
        f"/connections?workspace_id={ws}",
    ):
        assert (await client.get(path, headers=viewer.headers)).status_code == 200, path

    refused = [
        ("POST", "/runs", {"agent": "echo_ops", "goal": "x", "workspace_id": str(ws)}),
        ("POST", f"/workspaces/{ws}/discover", None),
        ("POST", f"/approvals/{approval_id}/decide", {"decision": "approve"}),
        ("POST", f"/approvals/{approval_id}/decide", {"decision": "reject"}),
        (
            "POST",
            "/incidents",
            {"workspace_id": str(ws), "resource_ids": [str(uuid.uuid4())]},
        ),
        (
            "POST",
            "/deploys",
            {
                "workspace_id": str(ws),
                "repo": {"connection_id": str(uuid.uuid4()), "repo": "a/b", "ref": "main"},
                "target_connection_id": str(uuid.uuid4()),
            },
        ),
        ("PATCH", f"/workspaces/{ws}", {"name": "x"}),
        ("POST", f"/workspaces/{ws}/members", {"github_login": "zed"}),
    ]
    for method, path, body in refused:
        r = await client.request(method, path, json=body, headers=viewer.headers)
        assert r.status_code == 403, (method, path, r.text)
    detail = (
        await client.post(
            f"/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=viewer.headers
        )
    ).json()["detail"]
    assert "approver" in detail

    async with session_factory() as session:
        run_id = await session.scalar(select(Approval.run_id).where(Approval.id == approval_id))
    stop = await client.post(f"/runs/{run_id}/stop", headers=viewer.headers)
    assert stop.status_code == 403
    # Viewers can still watch a run live.
    token = await client.post(f"/runs/{run_id}/stream-token", headers=viewer.headers)
    assert token.status_code == 200


async def test_approvers_decide_and_start_work_but_do_not_manage(
    client: httpx.AsyncClient, session_factory: SessionFactory, owner: Member
) -> None:
    ws = owner.workspace_id
    approver = await _add(client, session_factory, owner, 8004, "abe", "approver")
    approval_id = await _pending(session_factory, ws, ToolPolicy.write)
    r = await client.post(
        f"/approvals/{approval_id}/decide",
        json={"decision": "approve"},
        headers={**approver.headers, "x-client-ip": "198.51.100.4"},
    )
    assert r.status_code == 200 and r.json()["status"] == "approved"
    run = await client.post(
        "/runs", json={"agent": "echo_ops", "goal": "x", "workspace_id": str(ws)},
        headers=approver.headers,
    )  # fmt: skip
    assert run.status_code == 202
    for method, path, body in (
        ("PATCH", f"/workspaces/{ws}", {"name": "x"}),
        ("POST", f"/workspaces/{ws}/members", {"github_login": "zed"}),
        ("POST", "/connections/vercel", {"workspace_id": str(ws), "token": "t" * 24}),
    ):
        r = await client.request(method, path, json=body, headers=approver.headers)
        assert r.status_code == 403, path

    # The decision is in the audit log with who, what, when, and from where.
    async with session_factory() as session:
        event = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "approval.approved")
        )
    assert event is not None
    assert event.workspace_id == ws and event.actor_id == approver.user_id
    assert event.actor_label == "@abe" and event.ip == "198.51.100.4"
    assert event.target_id == str(approval_id)
    assert event.detail["tool"] == "target_apply" and event.detail["policy"] == "write"


async def test_emergency_approvals_can_be_kept_for_owners(
    client: httpx.AsyncClient, session_factory: SessionFactory, owner: Member
) -> None:
    ws = owner.workspace_id
    approver = await _add(client, session_factory, owner, 8005, "amy", "approver")
    first = await _pending(session_factory, ws, ToolPolicy.emergency)
    r = await client.post(
        f"/approvals/{first}/decide", json={"decision": "approve"}, headers=approver.headers
    )
    assert r.status_code == 403
    await client.patch(
        f"/workspaces/{ws}",
        json={"settings": {"emergency_approvers": "approvers"}},
        headers=owner.headers,
    )
    r = await client.post(
        f"/approvals/{first}/decide", json={"decision": "approve"}, headers=approver.headers
    )
    assert r.status_code == 200


def test_saved_members_setting_means_approvers() -> None:
    assert WorkspaceConfig.model_validate(
        {"emergency_approvers": "members"}
    ).emergency_approvers == ("approvers")


async def test_audit_log_lists_changes_newest_first(
    client: httpx.AsyncClient, session_factory: SessionFactory, owner: Member
) -> None:
    ws = owner.workspace_id
    viewer = await _add(client, session_factory, owner, 8006, "val", "viewer")
    await client.patch(
        f"/workspaces/{ws}/members/{viewer.user_id}",
        json={"role": "approver"},
        headers=owner.headers,
    )
    await client.patch(f"/workspaces/{ws}", json={"name": "Renamed"}, headers=owner.headers)
    await client.post(
        f"/workspaces/{ws}/members", json={"email": "new@example.com"}, headers=owner.headers
    )
    page = (await client.get(f"/workspaces/{ws}/audit", headers=viewer.headers)).json()
    actions = [e["action"] for e in page["items"]]
    assert actions == ["invite.created", "workspace.updated", "member.role_changed", "member.added"]
    changed = page["items"][2]
    assert changed["actor"] == "@olivia" and changed["detail"] == {
        "from": "viewer",
        "to": "approver",
    }
    assert page["items"][1]["detail"] == {"changed": ["name"]}
    assert page["items"][3]["detail"] == {"github_login": "val", "role": "viewer"}

    first = (await client.get(f"/workspaces/{ws}/audit?limit=2", headers=owner.headers)).json()
    assert len(first["items"]) == 2 and first["next_before"]
    rest = (
        await client.get(
            f"/workspaces/{ws}/audit",
            params={"limit": 10, "before": first["next_before"]},
            headers=owner.headers,
        )
    ).json()
    assert [e["action"] for e in rest["items"]] == ["member.role_changed", "member.added"]

    # Another workspace's log is not there for strangers.
    stranger = await _member(session_factory, 8007, "eve")
    assert (
        await client.get(f"/workspaces/{ws}/audit", headers=stranger.headers)
    ).status_code == 404


async def test_audit_events_go_with_their_workspace(session_factory: SessionFactory) -> None:
    from app.audit import record

    async with session_factory() as session:
        person = await _member(session_factory, 8008, "del")
        record(session, workspace_id=person.workspace_id, actor=None, action="x.y")
        await session.commit()
        workspace = await session.get_one(Workspace, person.workspace_id)
        await session.delete(workspace)
        await session.commit()
        left = await session.scalar(select(AuditEvent).where(AuditEvent.action == "x.y"))
    assert left is None
