"""Discovery engine: runs connectors, stores resources, infers edges, and writes the summary.

Job kinds (the worker dispatches both here):
- "discover_workspace" {run_id, workspace_id}: every connection of the workspace, in
  parallel, then the graph.
- "discover" {run_id, connection_id}: one connection (the Refresh button), then the graph.

Both drive a normal run (agent "discovery"), so progress streams over the run's SSE: one
"tool" step per phase (`discover_aws`, `discover_github`, ...), the Scout model call as an
"llm" step, and a final result line. A failure in one connection is recorded on it and in a
step; the others still finish. The job itself never retries (max_attempts=1): the run is the
record, and the user can run discovery again.

Storage: resources are upserted on (workspace_id, provider, external_id). A resource of a
connection that the latest complete discovery of that connection did not see is marked
`stale` (kept, shown dimmed, hidden by default). Edges are recomputed for the whole workspace
on every discovery from all non-stale resources and the latest repository facts.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import queue
from app.agents.scout.summary import summarize_system
from app.config import Settings, get_settings
from app.db.models import (
    TERMINAL_RUN_STATUSES,
    Connection,
    Edge,
    Resource,
    Run,
    RunStatus,
    StopReason,
    Workspace,
)
from app.db.session import SessionFactory
from app.discovery.types import (
    ConnectorError,
    ConnectorResult,
    DiscoveredResource,
    Progress,
    RepoFacts,
)
from app.graph.coverage import coverage
from app.graph.digest import digest, find_risks
from app.graph.edges import DECLARED_KEY, EdgeResult, infer_edges, key
from app.graph.sink import SINK_RULE
from app.graph.summary_check import check_summary
from app.llm.budget import PgUsageStore, RunBudget
from app.llm.client import ChatModel, TokenFactory
from app.llm.recorder import StepRecorder
from app.security.redact import summarize
from app.steps import record_step

logger = logging.getLogger("groundcrew.discovery")

AGENT = "discovery"
PREVIEW_KINDS = frozenset({"azure"})
# Where a GitHub repository resource keeps the facts edge inference needs.
FACTS_KEY = "iac_facts"

DiscoverFn = Callable[[Connection, Settings, Progress], Awaitable[ConnectorResult]]


def _now() -> datetime:
    return datetime.now(UTC)


# ----------------------------------------------------------------------- connectors


async def _discover_aws(
    conn: Connection, settings: Settings, progress: Progress
) -> ConnectorResult:
    from app.connectors.aws import AwsReader, connection_regions
    from app.connectors.credentials import ConnectionCredentialsProvider
    from app.discovery.aws import AwsDiscovery

    account = conn.config.get("account_id")
    if not conn.config.get("role_arn") or not account:
        raise ConnectorError("This AWS connection is not finished: paste the role ARN first.")
    with ConnectionCredentialsProvider([conn]) as credentials:
        session = await asyncio.to_thread(credentials.aws_session, conn.id)
    reader = AwsReader(session, settings, connection_regions(conn.config, settings))
    return await AwsDiscovery(
        reader, account, max_resources=settings.max_resources_per_connection, progress=progress
    ).run()


async def _discover_github(
    conn: Connection, settings: Settings, progress: Progress
) -> ConnectorResult:
    from app.connectors.github import get_github_app
    from app.discovery.github import discover_github

    repos = [str(r) for r in conn.config.get("repos") or []]
    if not repos:
        raise ConnectorError("No repositories are linked to this GitHub connection.")
    return await discover_github(
        get_github_app(), int(conn.config["installation_id"]), repos, progress=progress
    )


async def _discover_vercel(
    conn: Connection, settings: Settings, progress: Progress
) -> ConnectorResult:
    from app.connectors.vercel import VercelClient
    from app.discovery.vercel import discover_vercel
    from app.security.crypto import decrypt

    if not conn.secret_encrypted:
        raise ConnectorError("This Vercel connection has no token.")
    client = VercelClient(decrypt(conn.secret_encrypted), conn.config.get("team_id"), settings)
    return await discover_vercel(
        client, progress=progress, max_resources=settings.max_resources_per_connection
    )


async def _discover_k8s(
    conn: Connection, settings: Settings, progress: Progress
) -> ConnectorResult:
    from app.connectors.kubernetes import live_reader
    from app.discovery.kubernetes import discover_kubernetes
    from app.security.crypto import decrypt

    if not conn.secret_encrypted:
        raise ConnectorError("This Kubernetes connection has no kubeconfig.")
    limit = settings.max_resources_per_connection
    reader = live_reader(decrypt(conn.secret_encrypted), max_items=limit)
    try:
        cluster = str(conn.config.get("cluster") or conn.name)
        return await discover_kubernetes(reader, cluster, progress=progress, max_resources=limit)
    finally:
        reader.close()


async def _discover_preview(
    conn: Connection, settings: Settings, progress: Progress
) -> ConnectorResult:
    # TODO(azure-live): the live Azure reader is not built yet (fixtures only).
    raise ConnectorError(f"{conn.kind} discovery is in preview and does not run live yet.")


DEFAULT_CONNECTORS: dict[str, DiscoverFn] = {
    "aws": _discover_aws,
    "github": _discover_github,
    "vercel": _discover_vercel,
    "azure": _discover_preview,
    "k8s": _discover_k8s,
}


# --------------------------------------------------------------------------- starting


async def start_discovery(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    connection: Connection | None = None,
) -> Run:
    """Create the run and its job in the caller's transaction (the caller commits)."""
    if connection is None:
        goal = "Discover every connection in this workspace and map how they connect."
        kind, payload = "discover_workspace", {"workspace_id": str(workspace_id)}
    else:
        goal = f"Discover {connection.kind} connection {connection.name}."
        kind, payload = "discover", {"connection_id": str(connection.id)}
    run = Run(workspace_id=workspace_id, agent=AGENT, goal=goal)
    session.add(run)
    await session.flush()
    await queue.enqueue(session, kind, {"run_id": str(run.id), **payload}, max_attempts=1)
    return run


# ------------------------------------------------------------------------------ engine


class _StepWriter:
    """Serializes progress steps from parallel connectors onto one session (step seq is
    max+1 per run, so concurrent writers would collide)."""

    def __init__(self, session: AsyncSession, run: Run) -> None:
        self.session = session
        self.run = run
        self.lock = asyncio.Lock()

    async def __call__(
        self, tool: str, text: str, *, kind: str = "tool", latency_ms: int | None = None
    ) -> None:
        async with self.lock:
            await record_step(
                self.session,
                self.run,
                kind=kind,
                agent=AGENT,
                tool_name=tool,
                output_summary=text,
                latency_ms=latency_ms,
            )
            await self.session.commit()


class DiscoveryEngine:
    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        settings: Settings | None = None,
        llm: ChatModel | None = None,
        connectors: Mapping[str, DiscoverFn] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self._llm = llm
        self.connectors = dict(connectors or DEFAULT_CONNECTORS)

    def llm(self) -> ChatModel:
        if self._llm is None:
            self._llm = TokenFactory(self.settings)
        return self._llm

    async def handle_job(self, _sf: SessionFactory, payload: dict[str, Any]) -> None:
        await self.run(
            uuid.UUID(payload["run_id"]),
            connection_id=uuid.UUID(payload["connection_id"])
            if payload.get("connection_id")
            else None,
        )

    async def run(self, run_id: uuid.UUID, *, connection_id: uuid.UUID | None = None) -> None:
        async with self.session_factory() as session:
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None or run.status != RunStatus.queued:
                await session.rollback()
                return
            run.status = RunStatus.running
            run.started_at = _now()
            await session.commit()
            steps = _StepWriter(session, run)
            started = time.perf_counter()
            try:
                result = await self._run(session, run, steps, connection_id)
            except _StoppedError:
                await session.rollback()
                return
            except Exception as exc:
                logger.exception("discovery failed", extra={"run_id": str(run_id)})
                await session.rollback()
                await session.refresh(run)
                error = summarize(f"{type(exc).__name__}: {exc}", 1000) or type(exc).__name__
                await steps("discovery", error, kind="error")
                await self._finish(session, run, RunStatus.failed, StopReason.error, error=error)
                return
            seconds = time.perf_counter() - started
            run.result = f"{result} Took {seconds:.0f} s."
            await self._finish(session, run, RunStatus.succeeded, StopReason.completed)

    async def _finish(
        self,
        session: AsyncSession,
        run: Run,
        status: RunStatus,
        reason: StopReason,
        *,
        error: str | None = None,
    ) -> None:
        current = await session.scalar(select(Run.status).where(Run.id == run.id).with_for_update())
        if current in TERMINAL_RUN_STATUSES:
            await session.rollback()  # stopped by a user meanwhile
            return
        run.status, run.stop_reason, run.error, run.finished_at = status, reason, error, _now()
        await session.commit()

    async def _check_not_stopped(self, session: AsyncSession, run: Run) -> None:
        current = await session.scalar(select(Run.status).where(Run.id == run.id))
        await session.commit()
        if current in TERMINAL_RUN_STATUSES:
            raise _StoppedError

    async def _run(
        self,
        session: AsyncSession,
        run: Run,
        steps: _StepWriter,
        connection_id: uuid.UUID | None,
    ) -> str:
        query = select(Connection).where(Connection.workspace_id == run.workspace_id)
        if connection_id is not None:
            query = query.where(Connection.id == connection_id)
        connections = list(await session.scalars(query.order_by(Connection.created_at)))
        await session.commit()
        runnable = [
            c
            for c in connections
            if c.status == "connected"
            and (connection_id is not None or c.kind not in PREVIEW_KINDS)
        ]
        skipped = [c for c in connections if c not in runnable]
        if skipped:
            await steps(
                "discovery",
                "Skipped: "
                + ", ".join(
                    f"{c.name} ({'preview' if c.kind in PREVIEW_KINDS else c.status})"
                    for c in skipped
                ),
            )
        if not runnable and connection_id is None and not connections:
            await steps("discovery", "No connections yet. Connect AWS, GitHub, or Vercel first.")

        outcomes = await asyncio.gather(*(self._one(c, steps, run.id) for c in runnable))
        await self._check_not_stopped(session, run)

        total = 0
        for conn, outcome in zip(runnable, outcomes, strict=True):
            if isinstance(outcome, ConnectorResult):
                total += await self._store(session, run, conn, outcome)
        await steps("build_graph", "Inferring relationships from configuration and IaC")
        edge_result, resources = await self._rebuild_graph(session, run.workspace_id)
        by_rule: dict[str, int] = {}
        for e in edge_result.edges:
            by_rule[e.kind.value] = by_rule.get(e.kind.value, 0) + 1
        await steps(
            "build_graph",
            f"{len(resources)} resources, {len(edge_result.edges)} relationships: "
            + ", ".join(f"{n} {k}" for k, n in sorted(by_rule.items())),
        )
        await self._check_not_stopped(session, run)
        await self._summarize(session, run, steps, resources, edge_result)
        failed = [
            c.name
            for c, o in zip(runnable, outcomes, strict=True)
            if not isinstance(o, ConnectorResult)
        ]
        text = f"Discovered {total} resources from {len(runnable) - len(failed)} connections"
        if failed:
            text += f"; failed: {', '.join(failed)}"
        return text + f". {len(edge_result.edges)} relationships."

    async def _one(
        self, conn: Connection, steps: _StepWriter, run_id: uuid.UUID
    ) -> ConnectorResult | Exception:
        tool = f"discover_{conn.kind}"
        fn = self.connectors.get(conn.kind)
        started = time.perf_counter()

        async def progress(message: str) -> None:
            await steps(tool, message)

        try:
            if fn is None:
                raise ConnectorError(f"No connector for {conn.kind}.")
            await steps(tool, f"Starting {conn.name}")
            result = await asyncio.wait_for(
                fn(conn, self.settings, progress), timeout=self.settings.discovery_timeout_seconds
            )
        except Exception as exc:
            reason = (
                f"timed out after {self.settings.discovery_timeout_seconds:.0f} s"
                if isinstance(exc, TimeoutError)
                else summarize(str(exc) or type(exc).__name__, 500) or type(exc).__name__
            )
            await steps(tool, f"{conn.name} failed: {reason}", kind="error")
            await self._record_connection(conn.id, run_id, error=reason, started=started)
            return exc if isinstance(exc, Exception) else RuntimeError(reason)
        ms = round((time.perf_counter() - started) * 1000)
        notes = f" Notes: {' '.join(result.notes)}" if result.notes else ""
        await steps(tool, f"{conn.name}: {len(result.resources)} resources.{notes}", latency_ms=ms)
        return result

    async def _record_connection(
        self,
        connection_id: uuid.UUID,
        run_id: uuid.UUID,
        *,
        started: float,
        error: str | None = None,
        counts: dict[str, Any] | None = None,
    ) -> None:
        async with self.session_factory() as s:
            conn = await s.get(Connection, connection_id)
            if conn is None:
                return
            conn.last_discovery = {
                "at": _now().isoformat(),
                "run_id": str(run_id),
                "status": "failed" if error else "succeeded",
                "error": error,
                "duration_ms": round((time.perf_counter() - started) * 1000),
                **(counts or {}),
            }
            await s.commit()

    async def _store(
        self, session: AsyncSession, run: Run, conn: Connection, result: ConnectorResult
    ) -> int:
        started = time.perf_counter()
        seen_at = _now()
        facts = {f.repo.lower(): f for f in result.repos}
        declared: dict[str, list[dict[str, Any]]] = {}
        for edge in result.edges:
            declared.setdefault(edge.from_external_id, []).append(edge.model_dump(mode="json"))
        rows = []
        for r in _dedupe(result.resources):
            config = dict(r.config)
            if r.type == "github:repository" and r.name.lower() in facts:
                config[FACTS_KEY] = facts[r.name.lower()].model_dump(mode="json")
            if r.external_id in declared:
                config[DECLARED_KEY] = declared[r.external_id]
            rows.append(
                {
                    "id": uuid.uuid4(),
                    "workspace_id": run.workspace_id,
                    "connection_id": conn.id,
                    "provider": r.provider,
                    "type": r.type,
                    "external_id": r.external_id,
                    "account": r.account,
                    "region": r.region,
                    "name": r.name[:500],
                    "tags": r.tags,
                    "config": config,
                    "iac_path": r.iac_path,
                    "repo": r.repo,
                    "stale": False,
                    "last_seen_at": seen_at,
                }
            )
        for chunk in [rows[i : i + 200] for i in range(0, len(rows), 200)]:
            stmt = insert(Resource).values(chunk)
            excluded = stmt.excluded
            await session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_resources_workspace_external",
                    set_={
                        "connection_id": excluded.connection_id,
                        "type": excluded.type,
                        "account": excluded.account,
                        "region": excluded.region,
                        "name": excluded.name,
                        "tags": excluded.tags,
                        "config": excluded.config,
                        "repo": excluded.repo,
                        "stale": False,
                        "last_seen_at": excluded.last_seen_at,
                        "updated_at": seen_at,
                    },
                )
            )
        stale = 0
        if not result.truncated:
            # Everything of this connection the discovery did not see is stale now.
            marked = await session.execute(
                update(Resource)
                .where(
                    Resource.connection_id == conn.id,
                    Resource.stale.is_(False),
                    (Resource.last_seen_at.is_(None)) | (Resource.last_seen_at < seen_at),
                )
                .values(stale=True)
            )
            stale = int(getattr(marked, "rowcount", 0) or 0)
        await session.commit()
        await self._record_connection(
            conn.id,
            run.id,
            started=started,
            counts={
                "resources": len(rows),
                "stale": stale,
                "truncated": result.truncated,
                "notes": result.notes[:10],
            },
        )
        return len(rows)

    async def _rebuild_graph(
        self, session: AsyncSession, workspace_id: uuid.UUID
    ) -> tuple[EdgeResult, list[DiscoveredResource]]:
        rows = list(
            await session.scalars(
                select(Resource).where(
                    Resource.workspace_id == workspace_id, Resource.stale.is_(False)
                )
            )
        )
        resources = [to_discovered(r) for r in rows]
        repos = [
            RepoFacts.model_validate(r.config[FACTS_KEY])
            for r in rows
            if r.type == "github:repository" and isinstance(r.config.get(FACTS_KEY), dict)
        ]
        result = infer_edges(resources, repos)
        ids = {key(d): row.id for d, row in zip(resources, rows, strict=True)}
        # Inferred edges are recomputed; edges a deploy registered (ResourceSink) are kept.
        await session.execute(
            delete(Edge).where(
                Edge.workspace_id == workspace_id,
                Edge.evidence["rule"].astext.is_distinct_from(SINK_RULE),
            )
        )
        kept = set(
            (
                await session.execute(
                    select(Edge.from_id, Edge.to_id, Edge.kind).where(
                        Edge.workspace_id == workspace_id
                    )
                )
            ).tuples()
        )
        edge_rows = [
            {
                "id": uuid.uuid4(),
                "workspace_id": workspace_id,
                "from_id": ids[e.source],
                "to_id": ids[e.target],
                "kind": e.kind.value,
                "confidence": e.confidence.value,
                "evidence": {"text": e.evidence, "rule": e.rule, **e.details},
            }
            for e in result.edges
            if e.source in ids
            and e.target in ids
            and (ids[e.source], ids[e.target], e.kind.value) not in kept
        ]
        for chunk in [edge_rows[i : i + 500] for i in range(0, len(edge_rows), 500)]:
            await session.execute(insert(Edge).values(chunk))
        for d, row in zip(resources, rows, strict=True):
            link = result.iac.get(key(d))
            if link is not None and row.provider != "github":
                row.iac_path = link.path
                row.repo = link.repo
                row.config = {**row.config, "iac_address": link.address}
            row.coverage = coverage(row.type, key(d), result.edges)
        await session.commit()
        return result, resources

    async def _summarize(
        self,
        session: AsyncSession,
        run: Run,
        steps: _StepWriter,
        resources: list[DiscoveredResource],
        edges: EdgeResult,
    ) -> None:
        found = find_risks(resources, edges.edges)
        text: str | None = None
        error: str | None = None
        if resources:
            try:
                budget = await RunBudget.load(
                    session,
                    run,
                    PgUsageStore(self.session_factory, run.workspace_id),
                    self.settings,
                )
                await session.commit()
                text = await summarize_system(
                    self.llm(),
                    budget,
                    digest(resources, edges.edges, risks=found),
                    on_call=StepRecorder(session, run, "scout"),
                )
                checked = check_summary(text, resources, edges.edges)
                text = checked.text
                for bullet, why in checked.dropped:
                    await steps(
                        "check_summary",
                        f"Dropped from the summary because {why}: {bullet}",
                    )
            except Exception as exc:
                error = summarize(f"{type(exc).__name__}: {exc}", 500)
                await session.rollback()
                await steps("summarize_graph", f"Summary not written: {error}", kind="error")
        workspace = await session.get(Workspace, run.workspace_id, with_for_update=True)
        if workspace is not None:
            previous = (workspace.settings or {}).get("system_summary") or {}
            workspace.settings = {
                **(workspace.settings or {}),
                "system_summary": {
                    "text": text if text is not None else previous.get("text"),
                    "risks": [r.text for r in found],
                    "generated_at": _now().isoformat()
                    if text is not None
                    else previous.get("generated_at"),
                    "run_id": str(run.id),
                    "error": error,
                },
            }
        await session.commit()


class _StoppedError(Exception):
    pass


def _dedupe(resources: list[DiscoveredResource]) -> list[DiscoveredResource]:
    out: dict[tuple[str, str], DiscoveredResource] = {}
    for r in resources:
        out[(r.provider, r.external_id)] = r
    return list(out.values())


def to_discovered(row: Resource) -> DiscoveredResource:
    config = {k: v for k, v in (row.config or {}).items() if k != FACTS_KEY}
    return DiscoveredResource(
        provider=row.provider,
        type=row.type,
        external_id=row.external_id,
        name=row.name,
        account=row.account,
        region=row.region,
        tags=row.tags or {},
        config=config,
        iac_path=row.iac_path,
        repo=row.repo,
    )


async def discovery_job(session_factory: SessionFactory, payload: dict[str, Any]) -> None:
    """Worker handler for "discover" and "discover_workspace" jobs."""
    await DiscoveryEngine(session_factory).handle_job(session_factory, payload)
