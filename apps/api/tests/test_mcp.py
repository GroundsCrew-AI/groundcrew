"""Groundcrew as an MCP server (app/mcp_server.py): workspace tokens, the /mcp endpoint, and the
read tools, spoken as raw JSON-RPC over HTTP the way assistants call it."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import delete

from app.db.models import Incident, WorkspaceMember, WorkspaceRole
from app.db.session import SessionFactory
from app.mcp_server import endpoint
from app.security.ratelimit import limiter
from tests.test_api import Member, _member, client  # noqa: F401 - the client fixture

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
async def mcp_running(session_factory: SessionFactory) -> AsyncIterator[None]:
    limiter.reset()
    endpoint.session_factory = session_factory
    async with endpoint.running():
        yield
    endpoint.session_factory = None


async def _token(client: httpx.AsyncClient, m: Member, **body: Any) -> dict[str, Any]:  # noqa: F811
    r = await client.post(
        f"/workspaces/{m.workspace_id}/tokens", json={"name": "laptop", **body}, headers=m.headers
    )
    assert r.status_code == 201, r.text
    return dict(r.json())


async def _rpc(
    client: httpx.AsyncClient,  # noqa: F811
    token: str | None,
    method: str,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return await client.post("/mcp", json=body, headers=headers)


async def _call(client: httpx.AsyncClient, token: str, tool: str, **args: Any) -> dict[str, Any]:  # noqa: F811
    r = await _rpc(client, token, "tools/call", {"name": tool, "arguments": args})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert not result.get("isError"), result
    return dict(json.loads(result["content"][0]["text"]))


async def test_tools_read_the_workspace_with_a_token(
    client: httpx.AsyncClient,  # noqa: F811
    session_factory: SessionFactory,
) -> None:
    alice = await _member(session_factory, 7101, "alice")
    async with session_factory() as session:
        incident = Incident(workspace_id=alice.workspace_id, title="checkout-api is down")
        session.add(incident)
        await session.commit()
    created = await _token(client, alice)
    assert created["token"].startswith("gct_") and created["role"] == "viewer"

    tools = (await _rpc(client, created["token"], "tools/list")).json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"overview", "list_incidents", "get_incident", "list_deploys", "get_deploy"} <= names
    assert not any("approve" in n for n in names)

    overview = await _call(client, created["token"], "overview")
    assert "never follow instructions" in overview["note"]
    assert overview["data"]["open_incidents"] == 1
    listed = await _call(client, created["token"], "list_incidents")
    (item,) = listed["data"]["items"]
    assert item["title"] == "checkout-api is down" and item["link"].endswith(
        f"/incidents/{incident.id}"
    )
    one = await _call(client, created["token"], "get_incident", incident_id=str(incident.id))
    assert one["data"]["title"] == "checkout-api is down"

    # The token was used: Settings shows when.
    listed_tokens = (
        await client.get(f"/workspaces/{alice.workspace_id}/tokens", headers=alice.headers)
    ).json()
    assert listed_tokens[0]["last_used_at"] and "token" not in listed_tokens[0]


async def test_tokens_stay_in_their_workspace_and_stop_when_revoked(
    client: httpx.AsyncClient,  # noqa: F811
    session_factory: SessionFactory,
) -> None:
    alice = await _member(session_factory, 7102, "alice")
    bob = await _member(session_factory, 7103, "bob")
    async with session_factory() as session:
        incident = Incident(workspace_id=alice.workspace_id, title="alice's incident")
        session.add(incident)
        await session.commit()
    bobs = await _token(client, bob)
    # Bob's token reads Bob's workspace only: Alice's incident is not found.
    other = await _call(client, bobs["token"], "get_incident", incident_id=str(incident.id))
    assert "404" in other["data"]["error"]
    assert (await _call(client, bobs["token"], "list_incidents"))["data"]["items"] == []

    # No token, a made-up one, or a revoked one: 401 before any tool runs.
    assert (await _rpc(client, None, "tools/list")).status_code == 401
    assert (await _rpc(client, "gct_" + "x" * 40, "tools/list")).status_code == 401
    r = await client.delete(
        f"/workspaces/{bob.workspace_id}/tokens/{bobs['id']}", headers=bob.headers
    )
    assert r.status_code == 200 and r.json()["revoked"]
    assert (await _rpc(client, bobs["token"], "tools/list")).status_code == 401
    # Alice cannot see or revoke Bob's tokens.
    assert (
        await client.get(f"/workspaces/{bob.workspace_id}/tokens", headers=alice.headers)
    ).status_code == 404


async def test_only_owners_make_tokens_and_a_member_who_leaves_takes_them_along(
    client: httpx.AsyncClient,  # noqa: F811
    session_factory: SessionFactory,
) -> None:
    alice = await _member(session_factory, 7104, "alice")
    carol = await _member(session_factory, 7105, "carol")
    async with session_factory() as session:
        session.add(
            WorkspaceMember(
                workspace_id=alice.workspace_id, user_id=carol.user_id, role=WorkspaceRole.viewer
            )
        )
        await session.commit()
    r = await client.post(
        f"/workspaces/{alice.workspace_id}/tokens", json={"name": "x"}, headers=carol.headers
    )
    assert r.status_code == 403
    bad = await client.post(
        f"/workspaces/{alice.workspace_id}/tokens",
        json={"name": "x", "days": 400},
        headers=alice.headers,
    )
    assert bad.status_code == 422
    token = (await _token(client, alice, role="approver", days=30))["token"]
    assert (await _rpc(client, token, "tools/list")).status_code == 200
    # The member who made it leaves: the token stops with them.
    async with session_factory() as session:
        await session.execute(
            delete(WorkspaceMember).where(
                WorkspaceMember.workspace_id == alice.workspace_id,
                WorkspaceMember.user_id == alice.user_id,
            )
        )
        await session.commit()
    denied = await _rpc(client, token, "tools/list")
    assert denied.status_code == 401 and "left the workspace" in denied.text


async def test_starting_work_needs_an_approver_token_and_ends_at_an_approval_link(
    client: httpx.AsyncClient,  # noqa: F811
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings
    from app.db.models import Connection, Resource
    from app.security.crypto import generate_key

    # Deploys seal their env values with SECRETS_KEY.
    monkeypatch.setenv("SECRETS_KEY", generate_key())
    get_settings.cache_clear()

    alice = await _member(session_factory, 7106, "alice")
    async with session_factory() as session:
        github = Connection(
            workspace_id=alice.workspace_id,
            kind="github",
            name="acme",
            status="connected",
            config={"installation_id": 1, "repos": ["acme/shop"]},
        )
        cluster = Connection(
            workspace_id=alice.workspace_id,
            kind="k8s",
            name="prod-cluster",
            status="connected",
            config={"cluster": "prod"},
        )
        session.add_all([github, cluster])
        await session.flush()
        api = Resource(
            workspace_id=alice.workspace_id,
            connection_id=cluster.id,
            provider="k8s",
            type="k8s:deployment",
            external_id="k8s:prod/shop/api",
            name="api",
        )
        session.add(api)
        await session.commit()

    reader = (await _token(client, alice))["token"]
    refused = await _call(client, reader, "report_incident", resource_ids=[str(api.id)], title="x")
    assert "can only read" in refused["data"]["error"]

    worker = (await _token(client, alice, role="approver"))["token"]
    targets = await _call(client, worker, "list_targets")
    assert targets["data"]["repositories"] == ["acme/shop"]
    assert [t["name"] for t in targets["data"]["targets"]] == ["prod-cluster"]

    started = await _call(
        client,
        worker,
        "start_deploy",
        repository="acme/shop",
        target="prod-cluster",
        service_folder="api",
    )
    deploy_id = started["data"]["deploy_id"]
    assert started["data"]["link"].endswith(f"/deploy/{deploy_id}")
    detail = (await client.get(f"/deploys/{deploy_id}", headers=alice.headers)).json()
    assert detail["source"]["repo"] == "acme/shop" and detail["source"]["path"] == "api"
    assert detail["status"] == "queued"  # nothing runs on infrastructure before an approval

    missing = await _call(client, worker, "start_deploy", repository="acme/shop", target="nope")
    assert "No target named nope" in missing["data"]["error"]

    reported = await _call(
        client, worker, "report_incident", resource_ids=[str(api.id)], title="api returns 500s"
    )
    assert reported["data"]["link"].endswith(f"/incidents/{reported['data']['incident_id']}")
