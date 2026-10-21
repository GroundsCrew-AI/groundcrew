"""Tenant isolation: someone who is not a member of a workspace gets 404 from every route that
touches it, whatever id they send (path, query, or body), and nothing changes.

`CROSS_TENANT` lists one request per (method, path) in the OpenAPI schema. A new route fails
`test_every_route_is_covered` until it is added here, with the other workspace's ids, or to
`NOT_WORKSPACE_SCOPED` with the reason it touches no workspace.
"""

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import (
    Approval,
    ApprovalStatus,
    AuditEvent,
    Connection,
    Incident,
    Resource,
    Run,
    RunStatus,
    ToolPolicy,
    WorkspaceInvite,
    WorkspaceRole,
)
from app.db.session import SessionFactory, get_session_factory
from app.main import app
from app.security.auth import create_stream_token

from .test_api import Member, _member, _settings

pytestmark = pytest.mark.anyio


@dataclass
class Tenant:
    """Workspace A's ids, all owned by someone else than the caller."""

    workspace: uuid.UUID
    owner: uuid.UUID
    run: uuid.UUID
    approval: uuid.UUID
    deploy: uuid.UUID
    incident: uuid.UUID
    resource: uuid.UUID
    invite: uuid.UUID
    aws: uuid.UUID
    k8s: uuid.UUID
    vercel: uuid.UUID


Request = tuple[str, Callable[[Tenant], str], Callable[[Tenant], dict[str, Any] | None]]


def _none(_: Tenant) -> None:
    return None


# (method, OpenAPI path) -> (method, concrete URL, JSON body)
CROSS_TENANT: dict[tuple[str, str], Request] = {
    ("GET", "/workspaces/{workspace_id}"): ("GET", lambda t: f"/workspaces/{t.workspace}", _none),
    ("PATCH", "/workspaces/{workspace_id}"): (
        "PATCH",
        lambda t: f"/workspaces/{t.workspace}",
        lambda t: {"name": "taken"},
    ),
    ("GET", "/workspaces/{workspace_id}/runs"): (
        "GET",
        lambda t: f"/workspaces/{t.workspace}/runs",
        _none,
    ),
    ("GET", "/workspaces/{workspace_id}/approvals"): (
        "GET",
        lambda t: f"/workspaces/{t.workspace}/approvals",
        _none,
    ),
    ("GET", "/workspaces/{workspace_id}/summary"): (
        "GET",
        lambda t: f"/workspaces/{t.workspace}/summary",
        _none,
    ),
    ("GET", "/workspaces/{workspace_id}/audit"): (
        "GET",
        lambda t: f"/workspaces/{t.workspace}/audit",
        _none,
    ),
    ("GET", "/workspaces/{workspace_id}/members"): (
        "GET",
        lambda t: f"/workspaces/{t.workspace}/members",
        _none,
    ),
    ("POST", "/workspaces/{workspace_id}/members"): (
        "POST",
        lambda t: f"/workspaces/{t.workspace}/members",
        lambda t: {"github_login": "intruder", "role": "owner"},
    ),
    ("PATCH", "/workspaces/{workspace_id}/members/{user_id}"): (
        "PATCH",
        lambda t: f"/workspaces/{t.workspace}/members/{t.owner}",
        lambda t: {"role": "viewer"},
    ),
    ("DELETE", "/workspaces/{workspace_id}/members/{user_id}"): (
        "DELETE",
        lambda t: f"/workspaces/{t.workspace}/members/{t.owner}",
        _none,
    ),
    ("DELETE", "/workspaces/{workspace_id}/invites/{invite_id}"): (
        "DELETE",
        lambda t: f"/workspaces/{t.workspace}/invites/{t.invite}",
        _none,
    ),
    ("POST", "/workspaces/{workspace_id}/notifications/test"): (
        "POST",
        lambda t: f"/workspaces/{t.workspace}/notifications/test",
        _none,
    ),
    ("POST", "/workspaces/{workspace_id}/discover"): (
        "POST",
        lambda t: f"/workspaces/{t.workspace}/discover",
        _none,
    ),
    ("GET", "/workspaces/{workspace_id}/graph"): (
        "GET",
        lambda t: f"/workspaces/{t.workspace}/graph",
        _none,
    ),
    ("GET", "/resources/{resource_id}"): ("GET", lambda t: f"/resources/{t.resource}", _none),
    ("POST", "/runs"): (
        "POST",
        lambda t: "/runs",
        lambda t: {"agent": "echo_ops", "goal": "x", "workspace_id": str(t.workspace)},
    ),
    ("GET", "/runs/{run_id}"): ("GET", lambda t: f"/runs/{t.run}", _none),
    ("POST", "/runs/{run_id}/stop"): ("POST", lambda t: f"/runs/{t.run}/stop", _none),
    ("POST", "/runs/{run_id}/stream-token"): (
        "POST",
        lambda t: f"/runs/{t.run}/stream-token",
        _none,
    ),
    # A stream token the caller minted for themselves and the other workspace's run.
    ("GET", "/runs/{run_id}/events"): ("GET", lambda t: f"/runs/{t.run}/events?token=", _none),
    ("POST", "/approvals/{approval_id}/decide"): (
        "POST",
        lambda t: f"/approvals/{t.approval}/decide",
        lambda t: {"decision": "approve"},
    ),
    ("GET", "/connections"): ("GET", lambda t: f"/connections?workspace_id={t.workspace}", _none),
    ("GET", "/connections/github/installations"): (
        "GET",
        lambda t: f"/connections/github/installations?workspace_id={t.workspace}",
        _none,
    ),
    ("POST", "/connections/aws/start"): (
        "POST",
        lambda t: "/connections/aws/start",
        lambda t: {"workspace_id": str(t.workspace)},
    ),
    ("POST", "/connections/aws/complete"): (
        "POST",
        lambda t: "/connections/aws/complete",
        lambda t: {
            "connection_id": str(t.aws),
            "role_arn": "arn:aws:iam::123456789012:role/groundcrew-read-only",
        },
    ),
    ("POST", "/connections/{connection_id}/aws/tier"): (
        "POST",
        lambda t: f"/connections/{t.aws}/aws/tier",
        lambda t: {"tier": "deploy"},
    ),
    ("PUT", "/connections/{connection_id}/aws/regions"): (
        "PUT",
        lambda t: f"/connections/{t.aws}/aws/regions",
        lambda t: {"regions": ["us-east-2"]},
    ),
    ("POST", "/connections/{connection_id}/aws/operate"): (
        "POST",
        lambda t: f"/connections/{t.aws}/aws/operate",
        lambda t: {"enabled": False},
    ),
    ("POST", "/connections/{connection_id}/k8s/operate"): (
        "POST",
        lambda t: f"/connections/{t.k8s}/k8s/operate",
        lambda t: {"enabled": False},
    ),
    ("PUT", "/connections/{connection_id}/k8s/configmap-changes"): (
        "PUT",
        lambda t: f"/connections/{t.k8s}/k8s/configmap-changes",
        lambda t: {"enabled": False},
    ),
    ("PUT", "/connections/{connection_id}/k8s/deploy-settings"): (
        "PUT",
        lambda t: f"/connections/{t.k8s}/k8s/deploy-settings",
        lambda t: {"registry": "registry.example.com/team"},
    ),
    ("DELETE", "/connections/{connection_id}/k8s/deploy-settings"): (
        "DELETE",
        lambda t: f"/connections/{t.k8s}/k8s/deploy-settings",
        _none,
    ),
    ("POST", "/connections/{connection_id}/discover"): (
        "POST",
        lambda t: f"/connections/{t.vercel}/discover",
        _none,
    ),
    ("DELETE", "/connections/{connection_id}"): (
        "DELETE",
        lambda t: f"/connections/{t.vercel}",
        _none,
    ),
    ("POST", "/connections/github"): (
        "POST",
        lambda t: "/connections/github",
        lambda t: {"workspace_id": str(t.workspace), "installation_id": 1, "repos": ["a/b"]},
    ),
    ("POST", "/connections/vercel"): (
        "POST",
        lambda t: "/connections/vercel",
        lambda t: {"workspace_id": str(t.workspace), "token": "t" * 24},
    ),
    ("POST", "/connections/azure"): (
        "POST",
        lambda t: "/connections/azure",
        lambda t: {
            "workspace_id": str(t.workspace),
            "tenant_id": str(uuid.uuid4()),
            "client_id": str(uuid.uuid4()),
            "client_secret": "s" * 20,
            "subscription_id": str(uuid.uuid4()),
            "resource_group": "rg",
        },
    ),
    ("POST", "/connections/k8s"): (
        "POST",
        lambda t: "/connections/k8s",
        lambda t: {
            "workspace_id": str(t.workspace),
            "kubeconfig": "apiVersion: v1\nkind: Config\nclusters: []\n",
        },
    ),
    ("GET", "/deploys"): ("GET", lambda t: f"/deploys?workspace_id={t.workspace}", _none),
    ("POST", "/deploys"): (
        "POST",
        lambda t: "/deploys",
        lambda t: {
            "workspace_id": str(t.workspace),
            "repo": {"connection_id": str(t.vercel), "repo": "a/b", "ref": "main"},
            "target_connection_id": str(t.aws),
        },
    ),
    ("GET", "/deploys/{deploy_id}"): ("GET", lambda t: f"/deploys/{t.deploy}", _none),
    ("POST", "/deploys/{deploy_id}/env"): (
        "POST",
        lambda t: f"/deploys/{t.deploy}/env",
        lambda t: {"approval_id": str(t.approval)},
    ),
    ("GET", "/incidents"): ("GET", lambda t: f"/incidents?workspace_id={t.workspace}", _none),
    ("POST", "/incidents"): (
        "POST",
        lambda t: "/incidents",
        lambda t: {"workspace_id": str(t.workspace), "resource_ids": [str(t.resource)]},
    ),
    ("GET", "/incidents/{incident_id}"): ("GET", lambda t: f"/incidents/{t.incident}", _none),
    ("POST", "/incidents/{incident_id}/fixes/{fix_id}/retry"): (
        "POST",
        lambda t: f"/incidents/{t.incident}/fixes/fix-1/retry",
        _none,
    ),
    ("POST", "/incidents/{incident_id}/followups/{followup_id}/dismiss"): (
        "POST",
        lambda t: f"/incidents/{t.incident}/followups/{uuid.uuid4()}/dismiss",
        _none,
    ),
}

# Routes that touch no workspace, or only the caller's own data.
NOT_WORKSPACE_SCOPED = {
    ("GET", "/health"): "liveness",
    ("GET", "/ready"): "readiness",
    ("GET", "/me"): "the caller's own account",
    ("PATCH", "/me"): "the caller's own account",
    ("POST", "/me/password"): "the caller's own account",
    ("POST", "/workspaces"): "creates a workspace the caller owns",
    ("GET", "/connections/aws/template"): "the public connect template",
    ("GET", "/connections/aws/template-url"): "the public connect template URL",
}
# /auth/* takes only the web server's service token (tests/test_auth.py).
SERVICE_ONLY_PREFIX = "/auth/"


def _schema_routes() -> set[tuple[str, str]]:
    paths = app.openapi()["paths"]
    return {(m.upper(), p) for p, ops in paths.items() for m in ops if m != "parameters"}


def test_every_route_is_covered() -> None:
    routes = {r for r in _schema_routes() if not r[1].startswith(SERVICE_ONLY_PREFIX)}
    missing = routes - CROSS_TENANT.keys() - NOT_WORKSPACE_SCOPED.keys()
    assert not missing, f"add these routes to CROSS_TENANT or NOT_WORKSPACE_SCOPED: {missing}"
    stale = (CROSS_TENANT.keys() | NOT_WORKSPACE_SCOPED.keys()) - routes
    assert not stale, f"routes no longer in the API: {stale}"


@pytest.fixture
async def client(session_factory: SessionFactory) -> AsyncIterator[httpx.AsyncClient]:
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = lambda: _settings()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _tenant(sf: SessionFactory, alice: Member) -> Tenant:
    ws = alice.workspace_id
    async with sf() as session:
        run = Run(workspace_id=ws, agent="echo_ops", goal="g", status=RunStatus.waiting_approval)
        deploy = Run(workspace_id=ws, agent="deploy", goal="d", status=RunStatus.waiting_approval)
        session.add_all([run, deploy])
        await session.flush()
        approval = Approval(
            run_id=deploy.id,
            workspace_id=ws,
            tool_call_id="deploy-plan",
            tool_name="target_apply",
            policy=ToolPolicy.write,
            arguments={"plan_hash": "a" * 64},
            status=ApprovalStatus.pending,
        )
        aws = Connection(
            workspace_id=ws,
            kind="aws",
            name="AWS",
            status="connected",
            config={"external_id": "x", "account_id": "123456789012", "tier": "deploy"},
        )
        k8s = Connection(workspace_id=ws, kind="k8s", name="k8s", status="connected", config={})
        vercel = Connection(workspace_id=ws, kind="vercel", name="v", status="connected")
        invite = WorkspaceInvite(workspace_id=ws, github_login="someone", role=WorkspaceRole.viewer)
        session.add_all([approval, aws, k8s, vercel, invite])
        await session.flush()
        resource = Resource(
            workspace_id=ws,
            connection_id=aws.id,
            provider="aws",
            type="ecs:service",
            name="api",
            external_id="arn:aws:ecs:us-east-2:123456789012:service/c/api",
        )
        session.add(resource)
        await session.flush()
        incident = Incident(workspace_id=ws, title="Down", status="open", severity="high")
        session.add(incident)
        await session.commit()
        return Tenant(
            workspace=ws,
            owner=alice.user_id,
            run=run.id,
            approval=approval.id,
            deploy=deploy.id,
            incident=incident.id,
            resource=resource.id,
            invite=invite.id,
            aws=aws.id,
            k8s=k8s.id,
            vercel=vercel.id,
        )


def _stream_token(user_id: uuid.UUID, run_id: uuid.UUID) -> str:
    return create_stream_token(user_id, run_id, _settings())[0]


async def _counts(sf: SessionFactory) -> tuple[int, ...]:
    counts = []
    async with sf() as session:
        for table in (Run, Approval, Connection, Incident, WorkspaceInvite, AuditEvent):
            counts.append(int(await session.scalar(select(func.count()).select_from(table)) or 0))
    return tuple(counts)


async def test_other_workspaces_are_404_on_every_route(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    alice = await _member(session_factory, 9001, "alice")
    bob = await _member(session_factory, 9002, "bob")
    tenant = await _tenant(session_factory, alice)
    before = await _counts(session_factory)

    for (method, path), (verb, url, body) in CROSS_TENANT.items():
        target = url(tenant)
        if path == "/runs/{run_id}/events":
            target += _stream_token(bob.user_id, tenant.run)
        r = await client.request(verb, target, json=body(tenant), headers=bob.headers)
        assert r.status_code == 404, f"{method} {path}: {r.status_code} {r.text[:200]}"

    assert await _counts(session_factory) == before
    # And the owner still reaches them (the ids are real).
    r = await client.get(f"/workspaces/{tenant.workspace}", headers=alice.headers)
    assert r.status_code == 200
