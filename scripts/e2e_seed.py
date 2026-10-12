"""Seed the Playwright user and workspace (M8, fake mode). Safe to run more than once.

The web app's dev-only test sign-in (APP_ENV=dev, E2E_TEST_AUTH=1) signs in as GitHub id
990001 / e2e-tester; this creates that user, its workspace, three connections (GitHub, a kind
cluster, an observe-tier AWS account), and a small graph for the Map. Nothing here is real:
the pipeline runs in fake mode (PIPELINE_FAKE=1) against an in-memory cluster.

Run: DATABASE_URL=... uv run --project apps/api python scripts/e2e_seed.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from sqlalchemy import select  # noqa: E402

from app.accounts import upsert_user  # noqa: E402
from app.db.models import Connection, Edge, Resource, Workspace  # noqa: E402
from app.db.session import get_engine, get_session_factory  # noqa: E402
from app.security.auth import WebIdentity  # noqa: E402

TEST_USER = WebIdentity(github_id=990001, login="e2e-tester", name="E2E Tester")

CONNECTIONS = [
    (
        "github",
        "GroundsCrew-AI",
        {
            "account": "GroundsCrew-AI",
            "installation_id": 1,
            "repos": ["GroundsCrew-AI/legacy-flask-app"],
        },
    ),
    ("k8s", "kind e2e", {"cluster": "kind-e2e", "server": "https://kind-e2e.invalid:6443"}),
    ("aws", "AWS 111122223333", {"account_id": "111122223333", "tier": "observe"}),
]

RESOURCES = [
    (
        "aws",
        "ecs:service",
        "arn:aws:ecs:us-east-2:111122223333:service/demo/checkout-api",
        "checkout-api",
        {"running": 1, "desired": 1},
    ),
    (
        "aws",
        "rds:db",
        "arn:aws:rds:us-east-2:111122223333:db:checkout-orders-db",
        "checkout-orders-db",
        {"engine": "postgres"},
    ),
    (
        "aws",
        "logs:log-group",
        "arn:aws:logs:us-east-2:111122223333:log-group:/ecs/checkout-api",
        "/ecs/checkout-api",
        {},
    ),
]


async def seed() -> None:
    async with get_session_factory()() as session:
        user = await upsert_user(session, TEST_USER)
        workspace = await session.scalar(select(Workspace).where(Workspace.owner_id == user.id))
        if workspace is None:
            raise SystemExit("The test user has no workspace.")
        conns: dict[str, Connection] = {}
        for kind, name, config in CONNECTIONS:
            conn = await session.scalar(
                select(Connection).where(
                    Connection.workspace_id == workspace.id, Connection.name == name
                )
            )
            if conn is None:
                conn = Connection(
                    workspace_id=workspace.id,
                    kind=kind,
                    name=name,
                    config=config,
                    status="connected",
                )
                session.add(conn)
                await session.flush()
            conns[kind] = conn
        ids = {}
        for provider, rtype, external_id, name, config in RESOURCES:
            row = await session.scalar(
                select(Resource).where(
                    Resource.workspace_id == workspace.id, Resource.external_id == external_id
                )
            )
            if row is None:
                row = Resource(
                    workspace_id=workspace.id,
                    connection_id=conns["aws"].id,
                    provider=provider,
                    type=rtype,
                    external_id=external_id,
                    account="111122223333",
                    region="us-east-2",
                    name=name,
                    config=config,
                    coverage="observed",
                    last_seen_at=datetime.now(UTC),
                )
                session.add(row)
                await session.flush()
            ids[name] = row.id
        pairs = [
            ("checkout-api", "checkout-orders-db", "reads"),
            ("checkout-api", "/ecs/checkout-api", "logs_to"),
        ]
        for source, target, kind in pairs:
            edge = await session.scalar(
                select(Edge).where(Edge.from_id == ids[source], Edge.to_id == ids[target])
            )
            if edge is None:
                session.add(
                    Edge(
                        workspace_id=workspace.id,
                        from_id=ids[source],
                        to_id=ids[target],
                        kind=kind,
                        confidence="high",
                        evidence={"text": "seeded for e2e", "rule": "seed"},
                    )
                )
        await session.commit()
        print(f"e2e user {TEST_USER.login}, workspace {workspace.id}, {len(conns)} connections")
    await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(seed())
