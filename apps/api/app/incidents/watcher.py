"""The incident watcher: every INCIDENT_WATCH_INTERVAL_SECONDS, run the detector rules over
linked resources and open (or join) incidents.

Linked resources are ECS services (with the log groups they `logs_to`) and RDS databases (with
the log groups of the services that `reads` them, for the slow-query rule) on a connected AWS
connection, and Deployments and StatefulSets on a connected Kubernetes cluster
(app/incidents/k8s_rules.py:
rollout, pods, and their logs), not stale. All calls are read-only, through the connection's
guarded read-only session or its kubeconfig. One pass:

1. Read each resource's signals in parallel threads, each with a timeout; a failure is logged
   and skipped, never an incident.
2. Drop signals from before the last closed incident covering that resource, so stopped tasks
   ECS still lists do not reopen an incident Medic just resolved.
3. Merge signaled resources whose groups overlap and open one incident per merged set
   (`store.open_incident` dedupes against open incidents).

The job reschedules itself. `ensure_watch_job()` starts the chain (the worker calls it on start
when INCIDENT_WATCH_ENABLED=true); at most one watch job is ever queued.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import func, select, text

from app import queue
from app.agents.medic.events import Signal
from app.config import Settings, get_settings
from app.connectors.aws import AwsReader, connection_regions
from app.connectors.credentials import ConnectionCredentialsProvider
from app.connectors.kubernetes import KubeReader
from app.db.models import Connection, Edge, Incident, Job, JobStatus, Resource, Workspace
from app.db.session import SessionFactory
from app.incidents import k8s_rules, rules
from app.incidents.store import OPEN_STATUSES, Opened, open_incident, resource_group
from app.telemetry import k8s
from app.workspace_settings import get_config

logger = logging.getLogger("groundcrew.incidents")

WATCH_JOB: Final = "watch_incidents"
WATCH_TYPES = ("ecs:service", "rds:db", "lambda:function", "ec2:instance")
K8S_WATCH_TYPES = ("k8s:deployment", "k8s:statefulset")
CHECK_TIMEOUT_SECONDS = 30.0

ReaderFor = Callable[[Connection], AwsReader]
KubeFor = Callable[[Connection], KubeReader]


@dataclass
class Target:
    resource: Resource
    connection: Connection
    log_groups: list[str]


def default_reader_for(settings: Settings) -> ReaderFor:
    def reader_for(connection: Connection) -> AwsReader:
        provider = ConnectionCredentialsProvider([connection])
        return AwsReader(
            provider.aws_session(connection.id),
            settings,
            connection_regions(connection.config, settings),
        )

    return reader_for


def default_kube_for(connection: Connection) -> KubeReader:
    """A reader on the connection's stored kubeconfig (checked: inline credentials only)."""
    from app.connectors import kubernetes as kube
    from app.security.crypto import decrypt

    if not connection.secret_encrypted:
        raise LookupError(f"{connection.name} has no kubeconfig.")
    return kube.live_reader(decrypt(connection.secret_encrypted))


async def _targets(session_factory: SessionFactory) -> list[Target]:
    async with session_factory() as session:
        rows: list[Any] = list(
            (
                await session.execute(
                    select(Resource, Connection)
                    .join(Connection, Connection.id == Resource.connection_id)
                    .where(
                        (Resource.type.in_(WATCH_TYPES) & (Connection.kind == "aws"))
                        | (Resource.type.in_(K8S_WATCH_TYPES) & (Connection.kind == "k8s")),
                        Resource.stale.is_(False),
                        Connection.status == "connected",
                    )
                    .order_by(Resource.workspace_id, Resource.type, Resource.name)
                )
            ).all()
        )
        # Workspaces that turned the watcher off (settings) are skipped.
        workspace_ids = {r.workspace_id for r, _ in rows}
        if workspace_ids:
            off = {
                w.id
                for w in await session.scalars(
                    select(Workspace).where(Workspace.id.in_(workspace_ids))
                )
                if not get_config(w).incident_watch
            }
            rows = [(r, c) for r, c in rows if r.workspace_id not in off]
        services = [r.id for r, _ in rows if r.type == "ecs:service"]
        databases = [r.id for r, _ in rows if r.type == "rds:db"]
        logs: dict[uuid.UUID, list[str]] = {}
        if services:
            edges = await session.execute(
                select(Edge.from_id, Resource.name)
                .join(Resource, Resource.id == Edge.to_id)
                .where(
                    Edge.kind == "logs_to",
                    Edge.from_id.in_(services),
                    Resource.type == "logs:log-group",
                )
            )
            for from_id, name in edges:
                logs.setdefault(from_id, []).append(name)
        if databases:
            reads = await session.execute(
                select(Edge.from_id, Edge.to_id).where(
                    Edge.kind == "reads", Edge.to_id.in_(databases), Edge.from_id.in_(services)
                )
            )
            for service_id, db_id in reads.all():
                logs.setdefault(db_id, []).extend(logs.get(service_id, []))
    return [Target(r, c, sorted(set(logs.get(r.id, [])))) for r, c in rows]


def check(target: Target, reader: AwsReader, now: datetime) -> list[Signal]:
    """Blocking: every rule for one resource."""
    r = target.resource
    out: list[Signal] = []
    if r.type == "ecs:service":
        service, tasks = rules.fetch_ecs(
            reader, r.region, str(r.config.get("cluster_arn") or ""), r.name
        )
        if service is not None:
            out.extend(rules.ecs_signals(r.id, service, tasks, now))
        for group in target.log_groups:
            events = rules.fetch_logs(reader, r.region, group, now)
            signal = rules.log_signal(r.id, r.name, events, now)
            if signal is not None:
                out.append(signal)
    elif r.type == "rds:db":
        signal = rules.rds_check(reader, r.region, r.id, r.name, target.log_groups, now)
        if signal is not None:
            out.append(signal)
    elif r.type == "lambda:function":
        log_group = str(r.config.get("log_group") or f"/aws/lambda/{r.name}")
        out.extend(rules.lambda_check(reader, r.region, r.id, r.name, log_group, now))
    elif r.type == "ec2:instance":
        status = rules.fetch_ec2_status(reader, r.region, rules.instance_id(r.external_id))
        signal = rules.ec2_signal(r.id, r.name, status, now)
        if signal is not None:
            out.append(signal)
    return out


async def _check(
    target: Target, reader_for: ReaderFor, kube_for: KubeFor, now: datetime
) -> list[Signal]:
    try:
        if target.connection.kind == "k8s":
            r = target.resource
            namespace = str(r.config.get("namespace") or r.region or "default")
            kube = await asyncio.to_thread(kube_for, target.connection)
            try:
                return await asyncio.wait_for(
                    k8s_rules.check_workload(
                        kube, r.id, namespace, r.name, now, k8s.RESOURCE_KINDS[r.type]
                    ),
                    CHECK_TIMEOUT_SECONDS,
                )
            finally:
                close = getattr(kube, "close", None)
                if close is not None:
                    close()
        reader = await asyncio.to_thread(reader_for, target.connection)
        return await asyncio.wait_for(
            asyncio.to_thread(check, target, reader, now), CHECK_TIMEOUT_SECONDS
        )
    except Exception:
        logger.warning(
            "watcher check failed",
            extra={
                "resource": target.resource.name,
                "workspace": str(target.resource.workspace_id),
            },
            exc_info=True,
        )
        return []


async def _closed_at(
    session_factory: SessionFactory, workspace_id: uuid.UUID, resource_id: uuid.UUID
) -> datetime | None:
    """When the last closed incident covering this resource ended."""
    async with session_factory() as session:
        value = await session.scalar(
            select(func.max(Incident.window_end)).where(
                Incident.workspace_id == workspace_id,
                Incident.status.not_in(OPEN_STATUSES),
                Incident.group_resource_ids.contains([resource_id]),
            )
        )
    return value


async def _merge(
    session_factory: SessionFactory, workspace_id: uuid.UUID, signals: dict[uuid.UUID, list[Signal]]
) -> list[list[uuid.UUID]]:
    """Signaled resources whose groups overlap, merged (union-find over groups)."""
    async with session_factory() as session:
        groups = {rid: set(await resource_group(session, workspace_id, [rid])) for rid in signals}
    sets: list[tuple[set[uuid.UUID], list[uuid.UUID]]] = []
    for rid in signals:
        joined = [s for s in sets if s[0] & groups[rid]]
        group, members = set(groups[rid]), [rid]
        for s in joined:
            group |= s[0]
            members = s[1] + members
            sets.remove(s)
        sets.append((group, members))
    return [members for _, members in sets]


async def watch_once(
    session_factory: SessionFactory,
    *,
    reader_for: ReaderFor | None = None,
    kube_for: KubeFor | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> list[Opened]:
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    targets = await _targets(session_factory)
    found = await asyncio.gather(
        *(
            _check(t, reader_for or default_reader_for(settings), kube_for or default_kube_for, now)
            for t in targets
        )
    )
    by_workspace: dict[uuid.UUID, dict[uuid.UUID, list[Signal]]] = {}
    for target, signals in zip(targets, found, strict=True):
        if not signals:
            continue
        ws, rid = target.resource.workspace_id, target.resource.id
        closed = await _closed_at(session_factory, ws, rid)
        fresh = [s for s in signals if closed is None or s.at > closed]
        if fresh:
            by_workspace.setdefault(ws, {})[rid] = fresh
    opened: list[Opened] = []
    for ws, per_resource in by_workspace.items():
        for members in await _merge(session_factory, ws, per_resource):
            ordered = sorted((s for rid in members for s in per_resource[rid]), key=lambda s: s.at)
            async with session_factory() as session:
                result = await open_incident(
                    session,
                    workspace_id=ws,
                    resource_ids=members,
                    detected_by="watcher",
                    signals=ordered,
                    now=now,
                )
            opened.append(result)
            logger.info(
                "incident %s",
                "joined" if result.deduped else "opened",
                extra={"incident_id": str(result.incident.id), "workspace": str(ws)},
            )
    return opened


async def ensure_watch_job(
    session_factory: SessionFactory, *, delay: timedelta | None = None
) -> bool:
    """Queue the next watch pass unless one is already queued. True when it queued one."""
    async with session_factory() as session:
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('watch_incidents'))"))
        waiting = await session.scalar(
            select(func.count(Job.id)).where(Job.kind == WATCH_JOB, Job.status == JobStatus.queued)
        )
        if waiting:
            await session.rollback()
            return False
        await queue.enqueue(
            session,
            WATCH_JOB,
            {},
            run_after=datetime.now(UTC) + (delay or timedelta(0)),
            max_attempts=1,
        )
        await session.commit()
    return True


async def watch_incidents_job(session_factory: SessionFactory, payload: dict[str, Any]) -> None:
    """Worker handler: one pass, then the next one after the interval (if still enabled)."""
    settings = get_settings()
    try:
        await watch_once(session_factory, settings=settings)
    finally:
        # Same schedule, light: open fix PRs of resolved incidents (each at most every 2 min).
        try:
            from app.connectors.github import get_github_app
            from app.incidents.followups import track_followups

            github = get_github_app()
            await track_followups(session_factory, github if github.configured() else None)
        except Exception:
            logger.warning("follow-up tracking failed", exc_info=True)
        if settings.incident_watch_enabled:
            await ensure_watch_job(
                session_factory, delay=timedelta(seconds=settings.incident_watch_interval_seconds)
            )
