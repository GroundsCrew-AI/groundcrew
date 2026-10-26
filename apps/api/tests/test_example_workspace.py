"""The Example workspace (app.example): every signed-in user can open it as a viewer, nothing
can change it, and its copy carries none of the source's account ids, addresses, people, or
secrets."""

import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select

from app import example
from app.db.models import (
    Approval,
    ApprovalStatus,
    Connection,
    Edge,
    Event,
    Incident,
    IncidentFollowUp,
    Resource,
    Run,
    RunStatus,
    RunStep,
    ToolPolicy,
    User,
    Workspace,
)
from app.db.session import SessionFactory
from app.main import app
from tests.test_api import Member, _member, client  # noqa: F401 - the client fixture

pytestmark = pytest.mark.anyio

ACCOUNT = "881050425883"
EXTERNAL = "gc-ext-7f3a9c21d4"
PRIVATE_IP = "10.20.0.19"
PUBLIC_IP = "3.19.1.100"
EMAIL = "ops-lead@realcorp.io"
FERNET = "gAAAAABn" + "x" * 60

# Non-GET routes that are not about one workspace, and why the guard has nothing to check.
NOT_WORKSPACE_SCOPED = {
    "/auth/oauth": "sign-in",
    "/auth/sign-in": "sign-in",
    "/auth/sign-up": "sign-in",
    "/auth/resend-verification": "sign-in",
    "/auth/verify-email": "sign-in",
    "/auth/forgot-password": "sign-in",
    "/auth/reset-password": "sign-in",
    "/auth/email-link": "sign-in",
    "/auth/email-link/verify": "sign-in",
    "/me": "your own profile",
    "/me/password": "your own password",
    "/workspaces": "makes a new workspace of your own",
    "/connections/github/claim": "the workspace comes from a state that only an owner gets",
    "/admin/platform/signups": "platform operators only",
    "/github/webhook": "GitHub's signed events; copies keep no installation",
    "/agent/v1/enroll": "agents; the example has none",
    "/agent/v1/heartbeat": "agents",
    "/agent/v1/registry-login": "agents",
    "/agent/v1/inventory": "agents",
    "/agent/v1/events": "agents",
    "/agent/v1/commands/poll": "agents",
    "/agent/v1/commands/{command_id}/result": "agents",
    "/agent/v1/commands/{command_id}/secrets": "agents",
}


def _routes() -> list[APIRoute]:
    out: list[APIRoute] = []

    def walk(routes: list[Any]) -> None:
        for r in routes:
            inner = getattr(r, "original_router", None)
            if inner is not None:
                walk(inner.routes)
            elif isinstance(r, APIRoute):
                out.append(r)

    walk(app.routes)
    return out


def _writes() -> list[tuple[str, str]]:
    return sorted(
        (method, r.path) for r in _routes() for method in r.methods - {"GET", "HEAD", "OPTIONS"}
    )


async def _source(sf: SessionFactory, owner: Member) -> dict[str, uuid.UUID]:
    """A workspace with a bit of everything the example copies, full of values to scrub."""
    now = datetime.now(UTC)
    ws = owner.workspace_id
    async with sf() as session:
        user = await session.get(User, owner.user_id)
        assert user is not None
        user.email, user.name = EMAIL, "Rena Realname"
        conn = Connection(
            workspace_id=ws,
            kind="aws",
            name="prod",
            status="connected",
            config={
                "role_arn": f"arn:aws:iam::{ACCOUNT}:role/groundcrew-read",
                "external_id": EXTERNAL,
                "account_id": ACCOUNT,
            },
            secret_encrypted=FERNET,
        )
        github = Connection(
            workspace_id=ws,
            kind="github",
            name="acme",
            status="connected",
            config={"installation_id": 98765, "repos": ["acme/shop"]},
        )
        session.add_all([conn, github])
        await session.flush()
        api = Resource(
            workspace_id=ws,
            connection_id=conn.id,
            provider="aws",
            type="ecs:service",
            external_id=f"arn:aws:ecs:us-east-2:{ACCOUNT}:service/checkout/checkout-api",
            account=ACCOUNT,
            region="us-east-2",
            name="checkout-api",
            config={"private_ip": PRIVATE_IP, "host": "ip-10-20-0-19.us-east-2.compute.internal"},
        )
        db = Resource(
            workspace_id=ws,
            connection_id=conn.id,
            provider="aws",
            type="rds:db",
            external_id=f"arn:aws:rds:us-east-2:{ACCOUNT}:db:checkout",
            account=ACCOUNT,
            name="checkout",
        )
        session.add_all([api, db])
        await session.flush()
        session.add(
            Edge(
                workspace_id=ws,
                from_id=api.id,
                to_id=db.id,
                kind="reads",
                evidence={"text": f"DATABASE_URL points at {PUBLIC_IP}"},
            )
        )
        earlier = Incident(
            workspace_id=ws,
            title="checkout-api: secret changed",
            status="resolved",
            resolved_at=now - timedelta(days=2),
        )
        session.add(earlier)
        await session.flush()
        incident = Incident(
            workspace_id=ws,
            title=f"checkout-api unavailable on ip-10-20-0-19 ({ACCOUNT})",
            status="resolved",
            resolved_at=now,
            affected_resource_ids=[api.id],
            root_cause=f"{EMAIL} rotated the secret; the task at {PRIVATE_IP} lost the database",
            state={
                "evidence": [
                    {
                        "id": "ev-1",
                        "source": "cloudwatch_logs:/ecs/checkout-api",
                        "timestamp": now.isoformat(),
                        "line": f"connect to {PUBLIC_IP} refused",
                    }
                ],
                "memory": {
                    "incident_id": str(earlier.id),
                    "title": earlier.title,
                    "similarity": 0.95,
                    "reused": [],
                },
                "notes": [f"role arn:aws:iam::{ACCOUNT}:role/groundcrew-operate"],
            },
            remembered_at=now,
        )
        session.add(incident)
        await session.flush()
        medic = Run(
            workspace_id=ws,
            incident_id=incident.id,
            agent="medic",
            goal=f"Investigate {incident.title}",
            status=RunStatus.succeeded,
            messages=[{"role": "user", "content": f"evidence from {PUBLIC_IP}"}],
            cost_usd=Decimal("0.0068"),
        )
        deploy = Run(
            workspace_id=ws,
            agent="deploy",
            goal="Deploy acme/legacy-flask-app to prod",
            status=RunStatus.succeeded,
            state={"connection_id": str(conn.id), "url": f"http://{PUBLIC_IP}:8000"},
        )
        discovery = Run(
            workspace_id=ws, agent="discovery", goal="Discover", status=RunStatus.succeeded
        )
        session.add_all([medic, deploy, discovery])
        await session.flush()
        approval = Approval(
            run_id=medic.id,
            workspace_id=ws,
            tool_call_id="fix-1",
            tool_name="secret_rollback_version",
            policy=ToolPolicy.emergency,
            arguments={"secret_arn": f"arn:aws:secretsmanager:us-east-2:{ACCOUNT}:secret:db"},
            status=ApprovalStatus.approved,
            decided_by=owner.user_id,
            decided_at=now,
        )
        session.add(approval)
        await session.flush()
        session.add_all(
            [
                RunStep(
                    run_id=medic.id,
                    seq=1,
                    agent="medic",
                    kind="llm",
                    model_tier="ultra",
                    model_id="nvidia/Nemotron-3-Ultra",
                    output_summary=f"The secret changed; approved by Rena Realname ({EMAIL})",
                    cost_usd=Decimal("0.0068"),
                ),
                RunStep(
                    run_id=medic.id,
                    seq=2,
                    agent="medic",
                    kind="approval_decided",
                    approval_id=approval.id,
                    data={"by": "rena-real", "token": "gct_" + "a" * 40},
                ),
                RunStep(
                    run_id=deploy.id,
                    seq=1,
                    agent="builder",
                    kind="build_attempt",
                    data={"diff": "+RUN pip install psycopg2-binary"},
                ),
            ]
        )
        session.add(
            IncidentFollowUp(
                incident_id=incident.id,
                workspace_id=ws,
                repo="acme/shop",
                number=7,
                url="https://github.com/acme/shop/pull/7",
                base="main",
                installation_id=98765,
            )
        )
        session.add(
            Event(
                workspace_id=ws,
                resource_id=api.id,
                incident_id=incident.id,
                source="ecs",
                kind="deploy",
                occurred_at=now,
                payload={"task": f"arn:aws:ecs:us-east-2:{ACCOUNT}:task/abc"},
            )
        )
        await session.commit()
        return {
            "incident": incident.id,
            "earlier": earlier.id,
            "deploy": deploy.id,
            "connection": conn.id,
        }


async def _build(sf: SessionFactory, owner: Member, ids: dict[str, uuid.UUID]) -> uuid.UUID:
    async with sf() as session:
        workspace, plan = await example.build(
            session, owner.workspace_id, [ids["incident"]], [ids["deploy"]]
        )
        assert plan.problems == []
        await session.commit()
        return workspace.id


async def _example_ids(sf: SessionFactory, example_id: uuid.UUID) -> dict[str, uuid.UUID]:
    async with sf() as session:
        run = await session.scalar(
            select(Run).where(Run.workspace_id == example_id, Run.agent == "deploy")
        )
        approval = await session.scalar(select(Approval).where(Approval.workspace_id == example_id))
        conn = await session.scalar(
            select(Connection).where(
                Connection.workspace_id == example_id, Connection.kind == "aws"
            )
        )
        incident = await session.scalar(
            select(Incident).where(
                Incident.workspace_id == example_id, Incident.title.like("%unavailable%")
            )
        )
        assert run and approval and conn and incident
        return {
            "workspace_id": example_id,
            "run_id": run.id,
            "deploy_id": run.id,
            "approval_id": approval.id,
            "connection_id": conn.id,
            "incident_id": incident.id,
        }


def test_uuids_ending_in_twelve_digits_are_ids_not_accounts() -> None:
    """Found building the demo copy: new ids like ...-473012345678 were reported as AWS account
    ids, and the scrub would have rewritten a source id ending in digits."""
    rid = "c68317ca-707e-4c6d-8a1b-473012345678"
    text = f"arn:aws:iam::881050425883:role/x resource {rid}"
    assert example._accounts(text) == {"881050425883"}
    scrub = example.Scrubber()
    out = scrub.text(text)
    # The account is replaced; the id is mapped to its new id, whole.
    assert out == f"arn:aws:iam::{example.ACCOUNT}:role/x resource {scrub.ids[uuid.UUID(rid)]}"


async def test_the_copy_keeps_the_story_and_none_of_the_source_values(
    session_factory: SessionFactory,
) -> None:
    owner = await _member(session_factory, 9301, "rena-real")
    ids = await _source(session_factory, owner)
    example_id = await _build(session_factory, owner, ids)

    async with session_factory() as session:
        text = await example.example_text(session, example_id)
        assert await example.leaks(session, example_id, []) == []
        incidents = list(
            await session.scalars(select(Incident).where(Incident.workspace_id == example_id))
        )
        conn = await session.scalar(
            select(Connection).where(
                Connection.workspace_id == example_id, Connection.kind == "aws"
            )
        )
        approval = await session.scalar(select(Approval).where(Approval.workspace_id == example_id))
        followup = await session.scalar(
            select(IncidentFollowUp).where(IncidentFollowUp.workspace_id == example_id)
        )
        github = await session.scalar(
            select(Connection).where(
                Connection.workspace_id == example_id, Connection.kind == "github"
            )
        )
    for value in (
        ACCOUNT,
        EXTERNAL,
        PRIVATE_IP,
        PUBLIC_IP,
        EMAIL,
        "Rena Realname",
        "rena-real",
        "10-20-0-19",
        str(owner.workspace_id),
        str(ids["connection"]),
        str(ids["incident"]),
        "gAAAAA",
        "gct_",
    ):
        assert value not in text, value
    assert example.ACCOUNT in text and "203.0.113." in text and "person1@example.com" in text
    assert "psycopg2-binary" in text and "nvidia/Nemotron-3-Ultra" in text  # the story stays

    # The memory hit's past incident came along, under its new id.
    assert len(incidents) == 2
    earlier = next(i for i in incidents if i.title == "checkout-api: secret changed")
    hit = next(i for i in incidents if "unavailable" in i.title)
    assert re.fullmatch(
        rf"checkout-api unavailable on ip-203-0-113-\d+ \({example.ACCOUNT}\)", hit.title
    )
    assert hit.state["memory"]["incident_id"] == str(earlier.id)
    assert hit.embedding is None and hit.remembered_at is None
    assert conn is not None and conn.secret_encrypted is None
    assert conn.config["external_id"] == example.EXTERNAL_ID
    assert github is not None and "installation_id" not in github.config
    assert approval is not None and approval.decided_by is None
    assert approval.decided_via == example.PERSON
    assert followup is not None and followup.installation_id is None


async def test_building_again_replaces_the_copy_and_keeps_the_workspace(
    session_factory: SessionFactory,
) -> None:
    owner = await _member(session_factory, 9302, "rena-real")
    ids = await _source(session_factory, owner)
    first = await _build(session_factory, owner, ids)
    second = await _build(session_factory, owner, ids)
    assert first == second
    async with session_factory() as session:
        examples = list(await session.scalars(select(Workspace).where(Workspace.is_example)))
        incidents = list(
            await session.scalars(select(Incident).where(Incident.workspace_id == first))
        )
    assert len(examples) == 1 and len(incidents) == 2


def test_made_up_names_replace_real_ones_in_any_case() -> None:
    scrub = example.Scrubber(rename={"checkout": "shop", "Rena Real": "Sam Rivera"})
    assert scrub.text("Checkout-api reads CHECKOUT_URL; rena real approved") == (
        "Shop-api reads SHOP_URL; Sam Rivera approved"
    )


def test_names_behind_other_dashes_and_in_json_keys_are_renamed() -> None:
    """Found in the production preview: the model's summary wrote "groundcrew\u2011platform"
    (a non-breaking hyphen), and an IAM role kept a policy named after the app as a JSON key."""
    scrub = example.Scrubber(rename={"groundcrew-platform": "bastion", "checkout": "shop"})
    assert scrub.text("Security group groundcrew\u2011platform opens 443") == (
        "Security group bastion opens 443"
    )
    assert scrub.value({"inline_policies": {"checkout-vercel-api-url": [{"effect": "Allow"}]}}) == {
        "inline_policies": {"shop-vercel-api-url": [{"effect": "Allow"}]}
    }


def test_names_file_can_keep_the_people() -> None:
    names = example.Names(rename={"checkout": "shop"}, keep_people=True)
    assert names.keep_people and example.Names().keep_people is False


async def test_every_new_user_can_open_it_as_a_viewer(
    client: httpx.AsyncClient,  # noqa: F811
    session_factory: SessionFactory,
) -> None:
    owner = await _member(session_factory, 9303, "rena-real")
    example_id = await _build(session_factory, owner, await _source(session_factory, owner))
    newcomer = await _member(session_factory, 9304, "newcomer")
    ex = await _example_ids(session_factory, example_id)

    # A preview until published: only its owner sees it.
    (only,) = (await client.get("/me", headers=newcomer.headers)).json()["workspaces"]
    assert only["id"] == str(newcomer.workspace_id)
    hidden = await client.get(f"/incidents/{ex['incident_id']}", headers=newcomer.headers)
    assert hidden.status_code == 404
    mine = (await client.get("/me", headers=owner.headers)).json()["workspaces"]
    assert any(w["id"] == str(example_id) and w["role"] == "viewer" for w in mine)
    async with session_factory() as session:
        await example.set_published(session, True)
        await session.commit()

    me = (await client.get("/me", headers=newcomer.headers)).json()
    own, shown = me["workspaces"]
    assert own["id"] == str(newcomer.workspace_id) and not own["example"]  # theirs comes first
    assert shown == {
        "id": str(example_id),
        "name": example.NAME,
        "role": "viewer",
        "example": True,
    }
    for path in (
        f"/incidents/{ex['incident_id']}",
        f"/incidents?workspace_id={example_id}",
        f"/deploys/{ex['deploy_id']}",
        f"/runs/{ex['run_id']}",
        f"/workspaces/{example_id}/graph",
        f"/workspaces/{example_id}/summary",
        f"/connections?workspace_id={example_id}",
    ):
        r = await client.get(path, headers=newcomer.headers)
        assert r.status_code == 200, (path, r.text)
    # Nobody's membership shows: there is none.
    members = await client.get(f"/workspaces/{example_id}/members", headers=newcomer.headers)
    assert members.status_code in (200, 403)
    if members.status_code == 200:
        assert "newcomer" not in members.text and "rena" not in members.text


async def test_every_write_route_refuses_the_example_workspace(
    client: httpx.AsyncClient,  # noqa: F811
    session_factory: SessionFactory,
) -> None:
    """Enumerates the API's routes, so a new route is covered without editing this test."""
    owner = await _member(session_factory, 9305, "rena-real")
    example_id = await _build(session_factory, owner, await _source(session_factory, owner))
    ex = await _example_ids(session_factory, example_id)
    visitor = await _member(session_factory, 9306, "visitor")

    writes = _writes()
    paths = {p for _, p in writes}
    assert set(NOT_WORKSPACE_SCOPED) <= paths, set(NOT_WORKSPACE_SCOPED) - paths  # none stale
    checked = 0
    for method, path in writes:
        if path in NOT_WORKSPACE_SCOPED:
            continue
        url = re.sub(r"{(\w+)(?::\w+)?}", lambda m: str(ex.get(m.group(1), uuid.uuid4())), path)
        for headers in (visitor.headers, owner.headers):
            r = await client.request(
                method, url, json={"workspace_id": str(example_id)}, headers=headers
            )
            assert r.status_code == 403, (method, path, r.status_code, r.text)
            assert "read-only" in r.json()["detail"], (method, path)
        checked += 1
    assert checked > 30

    # Nothing changed.
    async with session_factory() as session:
        assert await example.leaks(session, example_id, []) == []
        conn = await session.get(Connection, ex["connection_id"])
        assert conn is not None and conn.status == "connected"
        assert (await session.get(Workspace, example_id)) is not None


async def test_owners_of_the_source_get_no_more_than_a_viewer_there(
    client: httpx.AsyncClient,  # noqa: F811
    session_factory: SessionFactory,
) -> None:
    owner = await _member(session_factory, 9307, "rena-real")
    example_id = await _build(session_factory, owner, await _source(session_factory, owner))
    me = (await client.get("/me", headers=owner.headers)).json()
    assert {"id": str(example_id), "name": example.NAME, "role": "viewer", "example": True} in me[
        "workspaces"
    ]
    # An assistant token needs an owner: refused before anything is made.
    r = await client.post(
        f"/workspaces/{example_id}/tokens", json={"name": "x"}, headers=owner.headers
    )
    assert r.status_code == 403
