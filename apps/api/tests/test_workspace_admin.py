"""Workspaces as a team: create, settings, members and invites, owner-only actions, and who
may approve emergency actions."""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest

from app.config import get_settings
from app.db.models import Approval, ApprovalStatus, Connection, Run, RunStatus, ToolPolicy
from app.db.session import SessionFactory, get_session_factory
from app.main import app
from tests.test_api import Member, _member, _settings

pytestmark = pytest.mark.anyio


@pytest.fixture
async def client(session_factory: SessionFactory) -> AsyncIterator[httpx.AsyncClient]:
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = lambda: _settings()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def owner(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 7001, "olivia")


async def _join(
    client: httpx.AsyncClient, owner: Member, login: str, role: str = "approver"
) -> None:
    r = await client.post(
        f"/workspaces/{owner.workspace_id}/members",
        json={"github_login": login, "role": role},
        headers=owner.headers,
    )
    assert r.status_code == 200, r.text


async def test_invite_applies_on_first_sign_in(
    client: httpx.AsyncClient, session_factory: SessionFactory, owner: Member
) -> None:
    ws = owner.workspace_id
    await _join(client, owner, "@Pat-Smith")
    listed = (await client.get(f"/workspaces/{ws}/members", headers=owner.headers)).json()
    assert [i["github_login"] for i in listed["invites"]] == ["pat-smith"]
    assert [m["github_login"] for m in listed["members"]] == ["olivia"]

    pat = await _member(session_factory, 7002, "Pat-Smith")  # first sign-in
    me = (await client.get("/me", headers=pat.headers)).json()
    assert {w["id"] for w in me["workspaces"]} == {str(pat.workspace_id), str(ws)}
    listed = (await client.get(f"/workspaces/{ws}/members", headers=owner.headers)).json()
    assert listed["invites"] == []
    assert [(m["github_login"], m["role"]) for m in listed["members"]] == [
        ("olivia", "owner"),
        ("Pat-Smith", "approver"),
    ]


async def test_existing_users_join_at_once_and_owners_are_protected(
    client: httpx.AsyncClient, session_factory: SessionFactory, owner: Member
) -> None:
    ws = owner.workspace_id
    sam = await _member(session_factory, 7003, "sam")
    await _join(client, owner, "sam")
    dup = await client.post(
        f"/workspaces/{ws}/members", json={"github_login": "SAM"}, headers=owner.headers
    )
    assert dup.status_code == 409

    # An approver can read but not change the workspace.
    assert (await client.get(f"/workspaces/{ws}", headers=sam.headers)).json()["role"] == "approver"
    denied = await client.patch(f"/workspaces/{ws}", json={"name": "x"}, headers=sam.headers)
    assert denied.status_code == 403 and "owner" in denied.json()["detail"]
    assert (await client.post(f"/workspaces/{ws}/members", json={"github_login": "zed"},
                              headers=sam.headers)).status_code == 403  # fmt: skip

    # The last owner cannot be demoted or removed; with a second owner they can.
    demote = await client.patch(
        f"/workspaces/{ws}/members/{owner.user_id}",
        json={"role": "approver"},
        headers=owner.headers,
    )
    assert demote.status_code == 409
    up = await client.patch(
        f"/workspaces/{ws}/members/{sam.user_id}", json={"role": "owner"}, headers=owner.headers
    )
    assert up.status_code == 200
    gone = await client.delete(f"/workspaces/{ws}/members/{owner.user_id}", headers=owner.headers)
    assert gone.status_code == 200  # olivia leaves; sam is owner now
    assert (await client.get(f"/workspaces/{ws}", headers=owner.headers)).status_code == 404

    # Strangers see nothing.
    stranger = await _member(session_factory, 7004, "eve")
    assert (
        await client.get(f"/workspaces/{ws}/members", headers=stranger.headers)
    ).status_code == 404


async def test_create_workspace_and_settings(client: httpx.AsyncClient, owner: Member) -> None:
    created = await client.post("/workspaces", json={"name": "  Client A  "}, headers=owner.headers)
    assert created.status_code == 201 and created.json()["role"] == "owner"
    wid = created.json()["id"]
    detail = (await client.get(f"/workspaces/{wid}", headers=owner.headers)).json()
    assert detail["name"] == "Client A"
    assert detail["settings"] == {
        "emergency_approvers": "owners",
        "incident_watch": True,
        "daily_usd_cap": None,
        "notifications": {
            "email": "owners",
            "incident_opened": True,
            "approval_needed": True,
            "incident_resolved": True,
            "incident_failed": True,
        },
    }
    r = await client.patch(
        f"/workspaces/{wid}",
        json={"settings": {"emergency_approvers": "approvers", "incident_watch": False,
                           "daily_usd_cap": 1.5}},
        headers=owner.headers,
    )  # fmt: skip
    assert r.status_code == 200 and r.json()["settings"]["daily_usd_cap"] == 1.5
    bad = await client.patch(f"/workspaces/{wid}", json={"settings": {"daily_usd_cap": -1}},
                             headers=owner.headers)  # fmt: skip
    assert bad.status_code == 422


async def _emergency_approval(sf: SessionFactory, workspace_id: uuid.UUID) -> uuid.UUID:
    async with sf() as session:
        run = Run(workspace_id=workspace_id, agent="medic", goal="g",
                  status=RunStatus.waiting_approval)  # fmt: skip
        session.add(run)
        await session.flush()
        approval = Approval(run_id=run.id, workspace_id=workspace_id, tool_call_id="c1",
                            tool_name="ecs_rollback_service", policy=ToolPolicy.emergency,
                            arguments={}, status=ApprovalStatus.pending)  # fmt: skip
        session.add(approval)
        await session.commit()
        return approval.id


async def test_emergency_approvals_need_an_owner_by_default(
    client: httpx.AsyncClient, session_factory: SessionFactory, owner: Member
) -> None:
    ws = owner.workspace_id
    mia = await _member(session_factory, 7005, "mia")
    await _join(client, owner, "mia")
    first = await _emergency_approval(session_factory, ws)
    r = await client.post(f"/approvals/{first}/decide", json={"decision": "approve"},
                          headers=mia.headers)  # fmt: skip
    assert r.status_code == 403
    # Turning a risky action down is always allowed.
    r = await client.post(f"/approvals/{first}/decide", json={"decision": "reject"},
                          headers=mia.headers)  # fmt: skip
    assert r.status_code == 200 and r.json()["status"] == "rejected"

    await client.patch(f"/workspaces/{ws}", json={"settings": {"emergency_approvers": "approvers"}},
                       headers=owner.headers)  # fmt: skip
    second = await _emergency_approval(session_factory, ws)
    r = await client.post(f"/approvals/{second}/decide", json={"decision": "approve"},
                          headers=mia.headers)  # fmt: skip
    assert r.status_code == 200 and r.json()["status"] == "approved"


async def test_only_owners_change_connections(
    client: httpx.AsyncClient, session_factory: SessionFactory, owner: Member
) -> None:
    ws = owner.workspace_id
    kim = await _member(session_factory, 7006, "kim")
    await _join(client, owner, "kim")
    async with session_factory() as session:
        conn = Connection(workspace_id=ws, kind="vercel", name="v", status="connected")
        session.add(conn)
        await session.commit()
        cid = conn.id
    assert (
        await client.get(f"/connections?workspace_id={ws}", headers=kim.headers)
    ).status_code == 200
    assert (await client.delete(f"/connections/{cid}", headers=kim.headers)).status_code == 403
    r = await client.post("/connections/vercel", json={"workspace_id": str(ws), "token": "t" * 24},
                          headers=kim.headers)  # fmt: skip
    assert r.status_code == 403
    assert (await client.delete(f"/connections/{cid}", headers=owner.headers)).status_code == 200


# --- per-workspace model spend ---------------------------------------------------------------


async def test_one_workspace_cannot_use_another_workspaces_budget(
    client: httpx.AsyncClient, session_factory: SessionFactory, owner: Member
) -> None:
    from decimal import Decimal

    from app.llm.budget import BudgetExceededError, PgUsageStore, RunBudget, workspace_daily_cap

    other = await _member(session_factory, 7010, "otto")
    await client.patch(
        f"/workspaces/{owner.workspace_id}",
        json={"settings": {"daily_usd_cap": 0.05}},
        headers=owner.headers,
    )
    busy = PgUsageStore(session_factory, owner.workspace_id)
    await busy.add(Decimal("0.06"), 1000)
    async with session_factory() as session:
        cap = await workspace_daily_cap(session, owner.workspace_id, _settings())
        default = await workspace_daily_cap(session, other.workspace_id, _settings())
    assert cap == Decimal("0.05") and default == Decimal(str(_settings().llm_daily_usd_cap))

    with pytest.raises(BudgetExceededError, match="spent today in this workspace"):
        await RunBudget(usage=busy, settings=_settings(), workspace_daily_usd_cap=cap).check_usage()
    quiet = PgUsageStore(session_factory, other.workspace_id)
    await RunBudget(
        usage=quiet, settings=_settings(), workspace_daily_usd_cap=default
    ).check_usage()

    summary = (await client.get(f"/workspaces/{owner.workspace_id}/summary",
                                headers=owner.headers)).json()  # fmt: skip
    assert summary["usd_today"] == pytest.approx(0.06) and summary["daily_usd_cap"] == 0.05
    assert summary["total_usd_cap"] is None and summary["open_incidents"] == 0
    other_summary = (await client.get(f"/workspaces/{other.workspace_id}/summary",
                                      headers=other.headers)).json()  # fmt: skip
    assert other_summary["usd_today"] == 0
