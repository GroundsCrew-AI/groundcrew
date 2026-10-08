"""ResourceSink: deploys report what they created, so it shows on the map right away.

Contract (docs/phase3-interfaces.md):

    class ResourceSink(Protocol):
        async def register_deployment(
            self, workspace_id: uuid.UUID, deployment: Deployment
        ) -> uuid.UUID: ...

`GraphResourceSink` upserts the deployed resource (same key as discovery: workspace,
provider, external_id, so a later discovery updates the same row) and a `deployed_by` edge
to whatever deployed it: a resource already in the graph (a GitHub workflow, for example) or,
by default, one "Groundcrew" node per workspace. Discovery recomputes inferred edges on every
run but keeps these (evidence rule `groundcrew_deploy`).

`NoopResourceSink` only logs; use it where no database is around (tests, dry runs).
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import Edge, Resource
from app.db.session import SessionFactory
from app.discovery.types import Confidence, DiscoveredResource, EdgeKind

logger = logging.getLogger("groundcrew.graph.sink")

SINK_RULE = "groundcrew_deploy"
DEPLOYER_EXTERNAL_ID = "groundcrew:deployer"


class Deployment(BaseModel):
    """What a deploy produced: the minimal shape the graph needs. Phase 4 owns the full record
    (runs.state); this only names the resource and how it got there."""

    # The deployed resource, with a stable external_id (ARN, "k8s:<ns>/<kind>/<name>", ...).
    resource: DiscoveredResource
    # The connection it belongs to, so its next discovery keeps it fresh. None: no connection.
    connection_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None  # the Groundcrew run that deployed it
    repo: str | None = None  # owner/name
    commit: str | None = None
    image: str | None = None
    # external_id of what deployed it, when that is already in the graph (a GitHub workflow).
    # None: the workspace's "Groundcrew" node.
    deployed_by: str | None = None
    summary: str | None = None  # one line for the edge evidence
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ResourceSink(Protocol):
    async def register_deployment(
        self, workspace_id: uuid.UUID, deployment: Deployment
    ) -> uuid.UUID: ...


class NoopResourceSink:
    async def register_deployment(
        self, workspace_id: uuid.UUID, deployment: Deployment
    ) -> uuid.UUID:
        logger.info(
            "deployment registered (no-op)",
            extra={
                "workspace_id": str(workspace_id),
                "external_id": deployment.resource.external_id,
            },
        )
        return uuid.UUID(int=0)


def _row(
    workspace_id: uuid.UUID, r: DiscoveredResource, connection_id: uuid.UUID | None, at: datetime
) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "provider": r.provider,
        "type": r.type,
        "external_id": r.external_id,
        "account": r.account,
        "region": r.region,
        "name": r.name[:500],
        "tags": r.tags,
        "config": r.config,
        "iac_path": r.iac_path,
        "repo": r.repo,
        "stale": False,
        "last_seen_at": at,
    }


class GraphResourceSink:
    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    async def register_deployment(
        self, workspace_id: uuid.UUID, deployment: Deployment
    ) -> uuid.UUID:
        """Upsert the deployed resource and its deployed_by edge; returns the resource id."""
        r, at = deployment.resource, deployment.at
        config = {
            **r.config,
            "last_deployment": {
                "at": at.isoformat(),
                "run_id": str(deployment.run_id) if deployment.run_id else None,
                "repo": deployment.repo,
                "commit": deployment.commit,
                "image": deployment.image,
            },
        }
        resource = r.model_copy(update={"config": config, "repo": r.repo or deployment.repo})
        async with self.session_factory() as session:
            stmt = insert(Resource).values(
                _row(workspace_id, resource, deployment.connection_id, at)
            )
            excluded = stmt.excluded
            await session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_resources_workspace_external",
                    set_={
                        "type": excluded.type,
                        "name": excluded.name,
                        "account": excluded.account,
                        "region": excluded.region,
                        "config": Resource.config.concat(excluded.config),
                        "repo": excluded.repo,
                        "stale": False,
                        "last_seen_at": excluded.last_seen_at,
                        "updated_at": at,
                    },
                )
            )
            resource_id = await self._id(session, workspace_id, r.provider, r.external_id)
            deployer_id = await self._deployer(session, workspace_id, deployment.deployed_by, at)
            await session.execute(
                delete(Edge).where(
                    Edge.from_id == resource_id,
                    Edge.to_id == deployer_id,
                    Edge.kind == EdgeKind.deployed_by.value,
                )
            )
            what = deployment.summary or (
                "Deployed by Groundcrew"
                + (f" from {deployment.repo}" if deployment.repo else "")
                + (f" at {deployment.commit[:7]}" if deployment.commit else "")
                + "."
            )
            session.add(
                Edge(
                    workspace_id=workspace_id,
                    from_id=resource_id,
                    to_id=deployer_id,
                    kind=EdgeKind.deployed_by.value,
                    confidence=Confidence.high.value,
                    evidence={
                        "text": what,
                        "rule": SINK_RULE,
                        "run_id": str(deployment.run_id) if deployment.run_id else None,
                        "commit": deployment.commit,
                        "at": at.isoformat(),
                    },
                )
            )
            await session.commit()
            return resource_id

    async def _id(
        self, session: Any, workspace_id: uuid.UUID, provider: str, external_id: str
    ) -> uuid.UUID:
        found = await session.scalar(
            select(Resource.id).where(
                Resource.workspace_id == workspace_id,
                Resource.provider == provider,
                Resource.external_id == external_id,
            )
        )
        if found is None:
            raise LookupError(f"{provider} resource {external_id} was not written.")
        return uuid.UUID(str(found))

    async def _deployer(
        self, session: Any, workspace_id: uuid.UUID, deployed_by: str | None, at: datetime
    ) -> uuid.UUID:
        if deployed_by:
            found = await session.scalar(
                select(Resource.id).where(
                    Resource.workspace_id == workspace_id, Resource.external_id == deployed_by
                )
            )
            if found is not None:
                return uuid.UUID(str(found))
        node = DiscoveredResource(
            provider="groundcrew",
            type="groundcrew:deployer",
            external_id=DEPLOYER_EXTERNAL_ID,
            name="Groundcrew",
        )
        stmt = insert(Resource).values(_row(workspace_id, node, None, at))
        await session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_resources_workspace_external",
                set_={"stale": False, "last_seen_at": at},
            )
        )
        return await self._id(session, workspace_id, node.provider, node.external_id)
