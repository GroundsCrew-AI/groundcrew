"""Deleting a connection, a workspace, or an account removes what Groundcrew stored for it, and
nothing of anyone else's. Settings says what each connection can reach."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.connectors.access import access_summary
from app.db.models import (
    Approval,
    ApprovalStatus,
    AuditEvent,
    Connection,
    Edge,
    Event,
    Job,
    JobStatus,
    Resource,
    Run,
    RunStatus,
    ToolPolicy,
    User,
    UserIdentity,
    Workspace,
    WorkspaceMember,
    WorkspaceQuotaUsage,
)
from app.db.session import SessionFactory, get_session_factory
from app.main import app

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
async def alice(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 9701, "alice")


async def _fill(
    sf: SessionFactory, ws: uuid.UUID, decided_by: uuid.UUID | None = None, quota: bool = True
) -> dict[str, uuid.UUID]:
    """A connection with two resources, an edge, an event, a run with a decided approval and a
    queued job, and today's quota use."""
    async with sf() as session:
        conn = Connection(workspace_id=ws, kind="vercel", name="v", status="connected",
                          secret_encrypted="gAAAA-token")  # fmt: skip  # noqa: S106
        session.add(conn)
        await session.flush()
        a = Resource(workspace_id=ws, connection_id=conn.id, provider="vercel", type="project",
                     name="web", external_id=f"prj-{uuid.uuid4().hex[:6]}")  # fmt: skip
        b = Resource(workspace_id=ws, connection_id=conn.id, provider="vercel", type="domain",
                     name="d", external_id=f"dom-{uuid.uuid4().hex[:6]}")  # fmt: skip
        session.add_all([a, b])
        await session.flush()
        session.add(Edge(workspace_id=ws, from_id=a.id, to_id=b.id, kind="calls"))
        session.add(Event(workspace_id=ws, resource_id=a.id, source="vercel", kind="deploy",
                          occurred_at=datetime.now(UTC)))  # fmt: skip
        run = Run(workspace_id=ws, agent="echo_ops", goal="g", status=RunStatus.queued)
        session.add(run)
        await session.flush()
        session.add(Approval(run_id=run.id, workspace_id=ws, tool_call_id="c", tool_name="t",
                             policy=ToolPolicy.write, arguments={}, status=ApprovalStatus.approved,
                             decided_by=decided_by))  # fmt: skip
        job = Job(kind="run_agent", payload={"run_id": str(run.id)})
        session.add(job)
        if quota:
            session.add(WorkspaceQuotaUsage(workspace_id=ws, day=datetime.now(UTC).date(),
                                            kind="tavily", count=2))  # fmt: skip
        await session.commit()
        return {"connection": conn.id, "resource": a.id, "run": run.id, "job": job.id}


async def _count(sf: SessionFactory, table: type, *where: object) -> int:
    async with sf() as session:
        return int(await session.scalar(select(func.count()).select_from(table).where(*where)) or 0)  # type: ignore[arg-type]


async def test_deleting_a_connection_removes_its_data_only(
    client: httpx.AsyncClient, session_factory: SessionFactory, alice: Member
) -> None:
    mine = await _fill(session_factory, alice.workspace_id)
    other = await _fill(session_factory, alice.workspace_id, quota=False)
    r = await client.delete(f"/connections/{mine['connection']}", headers=alice.headers)
    assert r.status_code == 200 and r.json()["removed_resources"] == 2
    assert "vercel.com/account/tokens" in r.json()["cleanup"][0]
    sf = session_factory
    assert await _count(sf, Connection, Connection.id == mine["connection"]) == 0
    assert await _count(sf, Resource, Resource.connection_id == mine["connection"]) == 0
    assert await _count(sf, Event, Event.resource_id == mine["resource"]) == 0
    assert await _count(sf, Event) == 1  # the other connection's
    assert await _count(sf, Edge) == 1
    assert await _count(sf, Resource, Resource.connection_id == other["connection"]) == 2


async def test_deleting_a_workspace_removes_everything_in_it(
    client: httpx.AsyncClient, session_factory: SessionFactory, alice: Member
) -> None:
    bob = await _member(session_factory, 9702, "bob")
    kept = await _fill(session_factory, bob.workspace_id)
    gone = await _fill(session_factory, alice.workspace_id)
    ws = alice.workspace_id
    await client.patch(f"/workspaces/{ws}", json={"name": "Shop"}, headers=alice.headers)
    await client.post(f"/workspaces/{ws}/members", json={"github_login": "bob", "role": "approver"},
                      headers=alice.headers)  # fmt: skip

    assert (await client.request("DELETE", f"/workspaces/{ws}", json={"confirm_name": "Shop"},
                                 headers=bob.headers)).status_code == 403  # fmt: skip
    wrong = await client.request("DELETE", f"/workspaces/{ws}", json={"confirm_name": "shop"},
                                 headers=alice.headers)  # fmt: skip
    assert wrong.status_code == 422
    r = await client.request("DELETE", f"/workspaces/{ws}", json={"confirm_name": "Shop"},
                             headers=alice.headers)  # fmt: skip
    assert r.status_code == 200, r.text
    assert r.json()["cleanup"] == [
        {"connection": "v", "kind": "vercel",
         "steps": ["Revoke the token you pasted at https://vercel.com/account/tokens."]}
    ]  # fmt: skip

    sf = session_factory
    for table, column in ((Connection, Connection.workspace_id), (Resource, Resource.workspace_id),
                          (Run, Run.workspace_id), (Approval, Approval.workspace_id),
                          (WorkspaceMember, WorkspaceMember.workspace_id),
                          (WorkspaceQuotaUsage, WorkspaceQuotaUsage.workspace_id),
                          (AuditEvent, AuditEvent.workspace_id)):  # fmt: skip
        assert await _count(sf, table, column == ws) == 0, table
    assert await _count(sf, Job, Job.id == gone["job"]) == 0
    assert await _count(sf, Job, Job.id == kept["job"], Job.status == JobStatus.queued) == 1
    assert await _count(sf, Run, Run.workspace_id == bob.workspace_id) == 1
    assert await _count(sf, AuditEvent, AuditEvent.action == "workspace.deleted",
                        AuditEvent.workspace_id.is_(None)) == 1  # fmt: skip
    assert (await client.get(f"/workspaces/{ws}", headers=alice.headers)).status_code == 404


async def test_deleting_an_account_alone(
    client: httpx.AsyncClient, session_factory: SessionFactory, alice: Member
) -> None:
    await _fill(session_factory, alice.workspace_id, decided_by=alice.user_id)
    preview = (await client.get("/me/account-deletion", headers=alice.headers)).json()
    assert [w["id"] for w in preview["deletes"]] == [str(alice.workspace_id)]
    assert preview["leaves"] == [] and preview["blocked_by"] == []
    bad = await client.request("DELETE", "/me", json={"confirm": "yes"}, headers=alice.headers)
    assert bad.status_code == 422
    r = await client.request("DELETE", "/me", json={"confirm": "Delete my account"},
                             headers=alice.headers)  # fmt: skip
    assert r.status_code == 204, r.text
    sf = session_factory
    assert await _count(sf, User, User.id == alice.user_id) == 0
    assert await _count(sf, UserIdentity, UserIdentity.user_id == alice.user_id) == 0
    assert await _count(sf, Workspace, Workspace.id == alice.workspace_id) == 0
    async with sf() as session:
        event = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "account.deleted")
        )
    assert event is not None and event.actor_id is None and event.actor_label == "@alice"
    assert (await client.get("/me", headers=alice.headers)).status_code == 401


async def test_a_shared_workspace_needs_another_owner_first(
    client: httpx.AsyncClient, session_factory: SessionFactory, alice: Member
) -> None:
    bob = await _member(session_factory, 9703, "bob")
    ws = alice.workspace_id
    await client.post(f"/workspaces/{ws}/members", json={"github_login": "bob", "role": "approver"},
                      headers=alice.headers)  # fmt: skip
    await _fill(session_factory, ws, decided_by=alice.user_id)
    confirm = {"confirm": "delete my account"}
    r = await client.request("DELETE", "/me", json=confirm, headers=alice.headers)
    assert r.status_code == 409 and "only owner of alice's workspace" in r.json()["detail"].lower()

    await client.patch(f"/workspaces/{ws}/members/{bob.user_id}", json={"role": "owner"},
                       headers=alice.headers)  # fmt: skip
    preview = (await client.get("/me/account-deletion", headers=alice.headers)).json()
    assert [w["id"] for w in preview["leaves"]] == [str(ws)]
    assert (
        await client.request("DELETE", "/me", json=confirm, headers=alice.headers)
    ).status_code == 204

    async with session_factory() as session:
        workspace = await session.get_one(Workspace, ws)
        assert workspace.owner_id == bob.user_id
        approval = await session.scalar(select(Approval).where(Approval.workspace_id == ws))
        assert approval is not None and approval.decided_by is None
        left = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.workspace_id == ws, AuditEvent.action == "member.left"
            )
        )
    assert left is not None and left.actor_label == "@alice"
    assert left.detail == {"reason": "account_deleted"}
    members = (await client.get(f"/workspaces/{ws}/members", headers=bob.headers)).json()
    assert [m["github_login"] for m in members["members"]] == ["bob"]


def test_access_says_what_each_connection_can_reach() -> None:
    s = _settings(discovery_regions="us-east-2")

    def conn(kind: str, **config: object) -> Connection:
        return Connection(workspace_id=uuid.uuid4(), kind=kind, name="x", config=config)

    observe = access_summary(conn("aws", external_id="e"), s)
    assert any("us-east-2" in line and "groundcrew-read-only" in line for line in observe)
    assert any("Never reads secret values" in line for line in observe)
    assert not any("Deploys" in line for line in observe)
    deploy = access_summary(
        conn("aws", tier="deploy", deploy_role_arn="arn:aws:iam::1:role/groundcrew-deploy",
             operate_role_arn="arn:aws:iam::1:role/groundcrew-operate"), s
    )  # fmt: skip
    assert any(line.startswith("Deploys after your approval") for line in deploy)
    assert any(line.startswith("Emergency fixes") for line in deploy)
    gh = access_summary(conn("github", repos=["a/b", "a/c"]), s)
    assert gh[0].startswith("Reads 2 repositories (a/b, a/c)") and "never merges" in gh[1]
    assert "Changes nothing in Vercel" in access_summary(conn("vercel"), s)[1]
    k8s = access_summary(conn("k8s", operate=True, operate_namespaces=["shop"]), s)
    assert "Never reads Secret data" in k8s[0] and "restart in shop" in k8s[1]
