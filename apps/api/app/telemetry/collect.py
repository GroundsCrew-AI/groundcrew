"""collect_evidence: from affected resources, through the graph, to the right readers.

1. Load the affected resources (workspace-scoped) and their neighbors: outgoing `uses` (secret),
   `logs_to` (log group), `reads` (database), `deployed_by` (workflow), `defined_in` (repo), and
   `calls` in both directions (callers break too). Tooling neighbors (app.graph.roles: the
   groundcrew:role=tooling tag, a state bucket, Groundcrew's role) are skipped; tooling the
   incident names directly is still read.
2. Pick a reader per resource type and run them in parallel, each with a timeout. A reader
   that fails leaves one line saying so; the others still count.
3. Return lines per source, the merged timeline, and the change markers the readers saw.

Read-only throughout: AWS through `CredentialsProvider.aws_session()` (the guarded read-only
session), GitHub through the App's GET-only client, Vercel through its read-only client,
Kubernetes Deployments through a `KubeReader` on the connection's kubeconfig.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Protocol

from sqlalchemy import or_, select

from app.agents.medic.events import ChangeMarker, ResourceRef
from app.config import Settings, get_settings
from app.connectors.aws import AwsReader, error_text
from app.connectors.credentials import ConnectionCredentialsProvider
from app.connectors.github import GitHubApp, get_github_app
from app.connectors.kubernetes import KubeReader
from app.connectors.vercel import VercelClient
from app.db.models import Connection, Edge, Resource
from app.db.session import SessionFactory
from app.graph.roles import role_of
from app.telemetry import cloudwatch_logs, ecs, github, k8s, rds, secrets, vercel
from app.telemetry.evidence import Line, ReaderOutput, line, render
from app.telemetry.timeline import timeline

READER_TIMEOUT_SECONDS = 45.0
MAX_NEIGHBORS = 30
OUTGOING = ("uses", "logs_to", "reads", "deployed_by", "defined_in")


class EvidenceClients(Protocol):
    """Where readers get their clients. Production: ConnectionClients; tests: fixtures."""

    def aws(self, connection_id: uuid.UUID) -> AwsReader: ...
    def github(self) -> GitHubApp: ...
    def vercel(self, connection_id: uuid.UUID) -> VercelClient: ...
    def installation_id(self, connection_id: uuid.UUID) -> int: ...
    def kube(self, connection_id: uuid.UUID) -> KubeReader: ...


class ConnectionClients:
    """Clients from the workspace's connections. aws() blocks (STS): call it in a thread."""

    def __init__(
        self,
        connections: list[Connection],
        settings: Settings | None = None,
        github_app: GitHubApp | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._connections = {c.id: c for c in connections}
        self._credentials = ConnectionCredentialsProvider(connections)
        self._github = github_app

    def _conn(self, connection_id: uuid.UUID) -> Connection:
        conn = self._connections.get(connection_id)
        if conn is None:
            raise LookupError(f"Connection {connection_id} is not in this workspace.")
        return conn

    def aws(self, connection_id: uuid.UUID) -> AwsReader:
        return AwsReader(self._credentials.aws_session(connection_id), self.settings)

    def github(self) -> GitHubApp:
        return self._github or get_github_app()

    def installation_id(self, connection_id: uuid.UUID) -> int:
        return int(self._conn(connection_id).config["installation_id"])

    def vercel(self, connection_id: uuid.UUID) -> VercelClient:
        from app.security.crypto import decrypt

        conn = self._conn(connection_id)
        if not conn.secret_encrypted:
            raise LookupError(f"{conn.name} has no Vercel token.")
        return VercelClient(
            decrypt(conn.secret_encrypted), conn.config.get("team_id"), self.settings
        )

    def kube(self, connection_id: uuid.UUID) -> KubeReader:
        from app.connectors import kubernetes
        from app.security.crypto import decrypt

        conn = self._conn(connection_id)
        if conn.kind != "k8s" or not conn.secret_encrypted:
            raise LookupError(f"{conn.name} has no kubeconfig.")
        return kubernetes.live_reader(decrypt(conn.secret_encrypted))

    def close(self) -> None:
        self._credentials.close()


@dataclass
class Evidence:
    sources: dict[str, list[Line]] = field(default_factory=dict)
    truncated: dict[str, bool] = field(default_factory=dict)
    # The graph resource each source reads (for evidence items' resource_id).
    source_resources: dict[str, uuid.UUID] = field(default_factory=dict)
    changes: list[ChangeMarker] = field(default_factory=list)
    affected: list[ResourceRef] = field(default_factory=list)
    neighbors: list[ResourceRef] = field(default_factory=list)
    # What went wrong collecting (resources not found). Shown to people, never to the model.
    notes: list[str] = field(default_factory=list)
    # Resources no reader covers (security groups, ECR repositories, ...): expected, and kept
    # out of `notes` so they never reach Medic's notes or the evidence the model sees.
    skipped: list[str] = field(default_factory=list)

    def texts(self) -> dict[str, str]:
        """The collect_evidence contract: text per source, plus `timeline`."""
        out = {key: render(lines) for key, lines in sorted(self.sources.items()) if lines}
        merged = timeline(self.sources.values())
        if merged:
            out["timeline"] = render(merged)
        return out


def _ref(r: Resource) -> ResourceRef:
    return ResourceRef(id=r.id, name=r.name, type=r.type, provider=r.provider)


async def load_resources(
    session_factory: SessionFactory, workspace_id: uuid.UUID, resource_ids: list[uuid.UUID]
) -> tuple[list[Resource], list[Resource], list[Connection]]:
    """The affected resources, their neighbors, and the workspace's connections."""
    async with session_factory() as session:
        affected = list(
            await session.scalars(
                select(Resource).where(
                    Resource.workspace_id == workspace_id, Resource.id.in_(resource_ids)
                )
            )
        )
        ids = [r.id for r in affected]
        edges = list(
            await session.scalars(
                select(Edge).where(
                    Edge.workspace_id == workspace_id,
                    or_(
                        Edge.from_id.in_(ids) & Edge.kind.in_(OUTGOING),
                        (Edge.kind == "calls") & (Edge.from_id.in_(ids) | Edge.to_id.in_(ids)),
                    ),
                )
            )
        )
        neighbor_ids = {e.to_id for e in edges} | {e.from_id for e in edges if e.kind == "calls"}
        neighbor_ids -= set(ids)
        found = (
            list(
                await session.scalars(
                    select(Resource)
                    .where(Resource.workspace_id == workspace_id, Resource.id.in_(neighbor_ids))
                    .order_by(Resource.type, Resource.name)
                )
            )
            if neighbor_ids
            else []
        )
        neighbors = [r for r in found if role_of(r.type, r.name, r.tags) == "app"][:MAX_NEIGHBORS]
        connections = list(
            await session.scalars(select(Connection).where(Connection.workspace_id == workspace_id))
        )
    return affected, neighbors, connections


Job = Callable[[], Awaitable[ReaderOutput]]


def plan_readers(
    resources: list[Resource], clients: EvidenceClients, since: datetime, until: datetime
) -> tuple[dict[str, tuple[Resource, Job]], list[str]]:
    """One reader per source key; resources no reader covers are listed in the second value."""
    jobs: dict[str, tuple[Resource, Job]] = {}
    notes: list[str] = []

    def aws_job(r: Resource, fn: Callable[[AwsReader], ReaderOutput]) -> Job:
        async def run() -> ReaderOutput:
            if r.connection_id is None:
                raise LookupError(f"{r.name} has no connection.")
            reader = await asyncio.to_thread(clients.aws, r.connection_id)
            return await asyncio.to_thread(fn, reader)

        return run

    for r in resources:
        key: str | None = None
        job: Job | None = None
        region = r.region
        if r.type == "ecs:service":
            key = f"ecs:{r.name}"
            cluster = str(r.config.get("cluster_arn") or "")
            job = aws_job(
                r,
                partial(
                    ecs.read_service,
                    region=region,
                    cluster=cluster,
                    service_name=r.name,
                    since=since,
                    until=until,
                ),
            )
        elif r.type == "logs:log-group":
            key = f"cloudwatch_logs:{r.name}"
            job = aws_job(
                r,
                partial(
                    cloudwatch_logs.read_log_group,
                    region=region,
                    log_group=r.name,
                    since=since,
                    until=until,
                ),
            )
        elif r.type == "rds:db":
            key = f"rds:{r.name}"
            job = aws_job(
                r,
                partial(rds.read_db, region=region, db_identifier=r.name, since=since, until=until),
            )
        elif r.type == "secretsmanager:secret":
            key = f"secrets:{r.name}"
            job = aws_job(
                r,
                partial(
                    secrets.read_secret,
                    region=region,
                    secret_id=r.external_id,
                    since=since,
                    until=until,
                ),
            )
        elif r.type in ("github:repository", "github:workflow") and (r.repo or r.name):
            repo = r.repo or r.name
            key = f"github:{repo}"
            conn = r.connection_id

            async def gh(repo: str = repo, conn: uuid.UUID | None = conn) -> ReaderOutput:
                if conn is None:
                    raise LookupError(f"{repo} has no GitHub connection.")
                return await github.read_repo(
                    clients.github(), clients.installation_id(conn), repo, since, until
                )

            job = gh
        elif r.type == "vercel:project":
            key = f"vercel:{r.name}"
            pid, conn = str(r.config.get("project_id") or ""), r.connection_id

            async def vc(
                pid: str = pid, conn: uuid.UUID | None = conn, name: str = r.name
            ) -> ReaderOutput:
                if conn is None:
                    raise LookupError(f"{name} has no Vercel connection.")
                return await vercel.read_project(clients.vercel(conn), pid, name, since, until)

            job = vc
        elif r.type == "k8s:deployment":
            key = f"k8s:{r.name}"
            ns, conn = str(r.config.get("namespace") or r.region or "default"), r.connection_id

            async def kd(
                ns: str = ns, conn: uuid.UUID | None = conn, name: str = r.name
            ) -> ReaderOutput:
                if conn is None:
                    raise LookupError(f"{name} has no Kubernetes connection.")
                reader = await asyncio.to_thread(clients.kube, conn)
                try:
                    return await k8s.read_workload(reader, ns, name, since, until)
                finally:
                    close = getattr(reader, "close", None)
                    if close is not None:
                        close()

            job = kd
        if key is None or job is None:
            notes.append(f"no reader for {r.type} {r.name}")
            continue
        jobs.setdefault(key, (r, job))
    return jobs, notes


async def _run(key: str, job: Job, until: datetime) -> ReaderOutput:
    reader = key.split(":", 1)[0]
    resource = key.split(":", 1)[1] if ":" in key else key
    try:
        return await asyncio.wait_for(job(), READER_TIMEOUT_SECONDS)
    except TimeoutError:
        message = f"reader timed out after {READER_TIMEOUT_SECONDS:.0f} s"
    except Exception as exc:  # one failed reader must not stop the rest
        message = f"reader failed: {error_text(exc)}"
    return ReaderOutput(lines=[line(until, reader, resource, message)], truncated=True)


async def collect(
    session_factory: SessionFactory,
    workspace_id: uuid.UUID,
    resource_ids: list[uuid.UUID],
    since: datetime,
    until: datetime,
    *,
    clients: EvidenceClients | None = None,
    settings: Settings | None = None,
) -> Evidence:
    until = max(min(until, datetime.now(UTC)), since)
    affected, neighbors, connections = await load_resources(
        session_factory, workspace_id, resource_ids
    )
    owned = ConnectionClients(connections, settings) if clients is None else None
    use: EvidenceClients = owned or clients  # type: ignore[assignment]
    try:
        jobs, skipped = plan_readers([*affected, *neighbors], use, since, until)
        keys = list(jobs)
        outputs = await asyncio.gather(*(_run(k, jobs[k][1], until) for k in keys))
    finally:
        if owned is not None:
            owned.close()
    evidence = Evidence(
        affected=[_ref(r) for r in affected],
        neighbors=[_ref(r) for r in neighbors],
        skipped=skipped,
    )
    missing = set(resource_ids) - {r.id for r in affected}
    if missing:
        evidence.notes.append(f"{len(missing)} resources not found in this workspace")
    for key, out in zip(keys, outputs, strict=True):
        resource = jobs[key][0]
        evidence.sources[key] = out.lines
        evidence.truncated[key] = out.truncated
        evidence.source_resources[key] = resource.id
        evidence.changes.extend(
            c if c.resource_id else c.model_copy(update={"resource_id": resource.id})
            for c in out.changes
        )
    evidence.changes = await _without_tooling(
        session_factory, workspace_id, evidence.changes, {r.id for r in affected}
    )
    evidence.changes.sort(key=lambda c: c.at)
    return evidence


async def _without_tooling(
    session_factory: SessionFactory,
    workspace_id: uuid.UUID,
    changes: list[ChangeMarker],
    named: set[uuid.UUID],
) -> list[ChangeMarker]:
    """Changes to tooling (app.graph.roles: groundcrew:role=tooling, a state bucket, ...) are not
    changes to the app; one the incident names directly stays."""
    ids = {c.resource_id for c in changes if c.resource_id and c.resource_id not in named}
    if not ids:
        return changes
    async with session_factory() as session:
        rows = await session.scalars(
            select(Resource).where(Resource.workspace_id == workspace_id, Resource.id.in_(ids))
        )
        tooling = {r.id for r in rows if role_of(r.type, r.name, r.tags) == "tooling"}
    return [c for c in changes if c.resource_id not in tooling]


class GraphEvidenceCollector:
    """The production `EvidenceCollector`: collect_evidence over the workspace's graph."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        clients: EvidenceClients | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.clients = clients
        self.settings = settings

    async def collect(
        self,
        workspace_id: uuid.UUID,
        resource_ids: list[uuid.UUID],
        since: datetime,
        until: datetime,
    ) -> Evidence:
        return await collect(
            self.session_factory,
            workspace_id,
            resource_ids,
            since,
            until,
            clients=self.clients,
            settings=self.settings,
        )

    async def __call__(
        self,
        workspace_id: uuid.UUID,
        resource_ids: list[uuid.UUID],
        since: datetime,
        until: datetime,
    ) -> dict[str, str]:
        return (await self.collect(workspace_id, resource_ids, since, until)).texts()
