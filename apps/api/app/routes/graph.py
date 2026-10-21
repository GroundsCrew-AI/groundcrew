"""The resource graph: start discovery, read the graph, read one resource with its evidence."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select

from app.accounts import NotFoundError, member_workspace
from app.db.models import Edge, Resource, Run, RunStatus, Workspace, WorkspaceRole
from app.discovery.engine import AGENT, start_discovery
from app.routes.deps import CurrentUser, SessionDep
from app.routes.discovery_schemas import (
    DeployOut,
    EdgeOut,
    GraphOut,
    Neighbor,
    ResourceDetail,
    ResourceEdgeOut,
    ResourceOut,
    SystemSummaryOut,
)
from app.routes.schemas import RunCreated

router = APIRouter(tags=["graph"])


async def _check_member(
    session: SessionDep,
    user: CurrentUser,
    workspace_id: uuid.UUID,
    minimum: WorkspaceRole = WorkspaceRole.viewer,
) -> None:
    try:
        await member_workspace(session, user, workspace_id, minimum)
    except NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found.") from None


@router.post("/workspaces/{workspace_id}/discover", status_code=status.HTTP_202_ACCEPTED)
async def discover_workspace(
    workspace_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> RunCreated:
    """Discover every connection, rebuild the graph, and write the system summary. Runs as a
    normal run: open /runs/{id} to watch it live."""
    await _check_member(session, user, workspace_id, WorkspaceRole.approver)
    active = await session.scalar(
        select(Run.id).where(
            Run.workspace_id == workspace_id,
            Run.agent == AGENT,
            Run.status.in_((RunStatus.queued, RunStatus.running)),
        )
    )
    if active is not None:
        return RunCreated(id=active, status=RunStatus.running)
    run = await start_discovery(session, workspace_id)
    await session.commit()
    return RunCreated(id=run.id, status=RunStatus.queued)


def _summary(workspace: Workspace | None) -> SystemSummaryOut | None:
    data = ((workspace.settings or {}) if workspace else {}).get("system_summary")
    if not isinstance(data, dict):
        return None
    return SystemSummaryOut.model_validate(data)


@router.get("/workspaces/{workspace_id}/graph")
async def graph(
    workspace_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    provider: Annotated[list[str] | None, Query()] = None,
    type: Annotated[list[str] | None, Query()] = None,
    include_stale: bool = False,
) -> GraphOut:
    """Resources and edges. Filters: provider and type (repeatable), include_stale."""
    await _check_member(session, user, workspace_id)
    query = select(Resource).where(Resource.workspace_id == workspace_id)
    if provider:
        query = query.where(Resource.provider.in_(provider))
    if type:
        query = query.where(Resource.type.in_(type))
    if not include_stale:
        query = query.where(Resource.stale.is_(False))
    nodes = list(
        await session.scalars(query.order_by(Resource.provider, Resource.type, Resource.name))
    )
    ids = {n.id for n in nodes}
    edges = [
        e
        for e in await session.scalars(
            select(Edge).where(Edge.workspace_id == workspace_id).order_by(Edge.kind)
        )
        if e.from_id in ids and e.to_id in ids
    ]
    stale = await session.scalar(
        select(func.count(Resource.id)).where(
            Resource.workspace_id == workspace_id, Resource.stale.is_(True)
        )
    )
    last = await session.scalar(
        select(Run)
        .where(Run.workspace_id == workspace_id, Run.agent == AGENT)
        .order_by(Run.created_at.desc())
        .limit(1)
    )
    workspace = await session.get(Workspace, workspace_id)
    return GraphOut(
        nodes=[ResourceOut.build(n) for n in nodes],
        edges=[EdgeOut.build(e) for e in edges],
        summary=_summary(workspace),
        last_run_id=last.id if last else None,
        last_run_status=last.status.value if last else None,
        last_run_at=(last.finished_at or last.created_at) if last else None,
        stale_count=int(stale or 0),
    )


def _deploys(resource: Resource, neighbors: list[Resource]) -> list[DeployOut]:
    c = resource.config or {}
    out: list[DeployOut] = []
    if resource.type == "ecs:service":
        for d in c.get("deployments") or []:
            td = str(d.get("task_definition") or "").rsplit("/", 1)[-1]
            out.append(
                DeployOut(
                    at=d.get("created_at"),
                    source="ECS deployment",
                    status=(d.get("rollout_state") or d.get("status") or "").lower() or None,
                    title=f"{d.get('status', '').lower()} deployment of {td}".strip(),
                    ref=td or None,
                    url=None,
                )
            )
    if resource.type in ("vercel:project", "vercel:deployment"):
        recent = c.get("recent") or ([c["latest_production"]] if c.get("latest_production") else [])
        for d in recent:
            out.append(
                DeployOut(
                    at=_ms_iso(d.get("created_at")),
                    source="Vercel production",
                    status=str(d.get("state") or "").lower() or None,
                    title=d.get("commit_message") or d.get("url"),
                    ref=(d.get("commit_sha") or "")[:7] or None,
                    url=f"https://{d['url']}" if d.get("url") else None,
                )
            )
    workflows = (
        [resource]
        if resource.type == "github:workflow"
        else [n for n in neighbors if n.type == "github:workflow"]
    )
    for wf in workflows:
        for r in (wf.config or {}).get("recent_runs") or []:
            out.append(
                DeployOut(
                    at=r.get("created_at"),
                    source=f"GitHub Actions {wf.name}",
                    status=r.get("conclusion") or r.get("status"),
                    title=r.get("name"),
                    ref=(r.get("head_sha") or "")[:7] or None,
                    url=r.get("html_url"),
                )
            )
    out.sort(key=lambda d: d.at or "", reverse=True)
    return out[:10]


def _ms_iso(value: Any) -> str | None:
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value / 1000).astimezone().isoformat()
    return str(value) if value else None


@router.get("/resources/{resource_id}")
async def get_resource(
    resource_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> ResourceDetail:
    resource = await session.get(Resource, resource_id)
    if resource is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found.")
    try:
        await member_workspace(session, user, resource.workspace_id)
    except NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found.") from None
    edges = list(
        await session.scalars(
            select(Edge).where(or_(Edge.from_id == resource.id, Edge.to_id == resource.id))
        )
    )
    other_ids = {e.to_id if e.from_id == resource.id else e.from_id for e in edges}
    others = {
        r.id: r for r in await session.scalars(select(Resource).where(Resource.id.in_(other_ids)))
    }
    edge_out: list[ResourceEdgeOut] = []
    for e in edges:
        outgoing = e.from_id == resource.id
        other = others.get(e.to_id if outgoing else e.from_id)
        if other is None:
            continue
        edge_out.append(
            ResourceEdgeOut(
                **EdgeOut.build(e).model_dump(),
                direction="out" if outgoing else "in",
                other=Neighbor(
                    id=other.id, name=other.name, type=other.type, provider=other.provider
                ),
            )
        )
    order = {"high": 0, "medium": 1, "low": 2}
    edge_out.sort(key=lambda e: (order[e.confidence], e.kind, e.other.name))
    deployed_by = [
        others[e.to_id] for e in edges if e.kind == "deployed_by" and e.from_id == resource.id
    ]
    return ResourceDetail(
        **ResourceOut.build(resource).model_dump(),
        tags=resource.tags or {},
        config=ResourceDetail.safe_config(resource),
        edges=edge_out,
        recent_deploys=_deploys(resource, deployed_by),
    )
