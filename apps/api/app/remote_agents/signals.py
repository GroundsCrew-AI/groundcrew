"""What agents report about their services' live state, and the detector rules over it.

Every 10 seconds an agent posts each service's state (running or not, restarts, the last exit,
an OOM kill, consecutive failed health checks) and the events it saw since (state changes,
failed and recovered health checks, error log lines redacted on the host, image changes). The
state replaces the last one; events are kept 7 days for Medic's evidence.

Rules, per service (the same names the cloud rules use, so Medic treats them alike):
- `agent_service_down`: the container or unit is not running (exited, restarting, failed).
- `health_check`: HEALTH_FAILURES failed health checks in a row.
- `log_error_spike`: LOG_SPIKE_LINES or more error lines in the last LOG_SPIKE_WINDOW.

An incident opens when a rule *starts* firing for a service (the agent's `firing` set), not on
every report, and only with INCIDENT_WATCH_ENABLED, like the watcher's cloud rules.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.medic.events import DetectorRule, Signal
from app.config import Settings
from app.db.models import Agent, AgentEvent, Connection, Resource
from app.remote_agents import inventory as inv
from app.security.redact import redact

logger = logging.getLogger(__name__)

HEALTH_FAILURES = 3
LOG_SPIKE_LINES = 10
LOG_SPIKE_WINDOW = timedelta(minutes=5)
RETENTION = timedelta(days=7)
MAX_EVENTS = 500
# Container states (Docker) and unit states (systemd) that mean the service runs.
RUNNING = frozenset({"running", "active"})

EventKind = Literal["state", "health", "restart", "log", "deploy", "command"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServiceState(_Strict):
    name: str = Field(max_length=100)
    found: bool
    # docker: created | running | paused | restarting | removing | exited | dead;
    # systemd: active | inactive | failed | activating | deactivating | reloading
    state: str = Field(default="", max_length=32)
    restarts: int = Field(default=0, ge=0)
    exit_code: int | None = None
    oom_killed: bool = False
    started_at: datetime | None = None
    # Consecutive failed health checks (0 once one passes) and the last failure's reason.
    health_failures: int = Field(default=0, ge=0)
    health_detail: str | None = Field(default=None, max_length=300)
    image_digest: str | None = Field(default=None, max_length=100)
    # Docker and Compose: the image reference and local id it runs, and the one it ran before
    # that is still on the host (what a rollback goes back to). Kubernetes and ECS have no local
    # id and send the full reference, e.g. an ECR image with a commit tag, as the id.
    image: str | None = Field(default=None, max_length=500)
    image_id: str | None = Field(default=None, max_length=500)
    previous_image: str | None = Field(default=None, max_length=500)
    previous_image_id: str | None = Field(default=None, max_length=500)


class EventIn(_Strict):
    service: str = Field(max_length=100)
    at: datetime
    kind: EventKind
    message: str = Field(max_length=2000)


class EventsIn(_Strict):
    states: list[ServiceState] = Field(default_factory=list, max_length=inv.MAX_SERVICES)
    events: list[EventIn] = Field(default_factory=list, max_length=MAX_EVENTS)


def describe(s: ServiceState) -> str:
    """One line for a service's state, also an ActionPlan's `current`."""
    if not s.found:
        return f"{s.name} not found"
    text = f"{s.name} {s.state or 'unknown'}"
    if s.state not in RUNNING and s.exit_code is not None:
        text += f" (exit code {s.exit_code}{', OOM killed' if s.oom_killed else ''})"
    return text


def image_label(ref: str | None, image_id: str | None) -> str:
    """ "web:2@0a1b2c3d4e5f": the reference and the short local id (the same text the agent
    uses in its image change events)."""
    if image_id and image_id == ref:
        # Kubernetes and ECS: the "id" is the reference itself (the registry holds the image).
        return ref
    short = (image_id or "").removeprefix("sha256:")[:12]
    return f"{ref or '?'}@{short}"


def service_signals(
    s: ServiceState, error_lines: int, resource: Resource, now: datetime
) -> list[Signal]:
    """The rules that fire for one service now."""
    out: list[Signal] = []

    def signal(rule: DetectorRule, detail: str, down: bool = False) -> None:
        out.append(
            Signal(
                rule=rule,
                resource_id=resource.id,
                resource_name=resource.name,
                detail=detail[:500],
                at=now,
                down=down,
            )
        )

    if not s.found:
        return out
    if s.state not in RUNNING:
        cfg = resource.config or {}
        where = inv.location(cfg.get("runtime"), cfg.get("target"), resource.account or "")
        signal("agent_service_down", f"{describe(s)} on {where}", down=True)
    elif s.health_failures >= HEALTH_FAILURES:
        signal(
            "health_check",
            f"{s.name}: {s.health_failures} health checks failed in a row"
            + (f" ({s.health_detail})" if s.health_detail else ""),
            down=True,
        )
    if error_lines >= LOG_SPIKE_LINES:
        signal(
            "log_error_spike",
            f"{s.name}: {error_lines} error lines in the last "
            f"{int(LOG_SPIKE_WINDOW.total_seconds() // 60)} minutes",
        )
    return out


async def _service_resources(session: AsyncSession, conn: Connection) -> dict[str, Resource]:
    rows = await session.scalars(
        select(Resource).where(
            Resource.connection_id == conn.id,
            Resource.type == inv.SERVICE_TYPE,
            Resource.stale.is_(False),
        )
    )
    prefix = inv.service_id(conn, "")
    return {r.external_id.removeprefix(prefix): r for r in rows}


async def ingest(
    session: AsyncSession, agent: Agent, conn: Connection, body: EventsIn, settings: Settings
) -> list[Signal]:
    """Store the report and return the signals that started firing (the caller opens the
    incident after committing). Commits."""
    now = datetime.now(UTC)
    for e in body.events:
        session.add(
            AgentEvent(
                agent_id=agent.id,
                service=e.service,
                at=min(e.at, now),
                kind=e.kind,
                message=redact(" ".join(e.message.split()))[:500],
            )
        )
    await session.execute(
        delete(AgentEvent).where(AgentEvent.agent_id == agent.id, AgentEvent.at < now - RETENTION)
    )
    if body.states:
        agent.states = {s.name: s.model_dump(mode="json") for s in body.states}
        agent.states_at = now
    await session.flush()
    resources = await _service_resources(session, conn)
    counts = dict(
        (
            await session.execute(
                select(AgentEvent.service, func.count())
                .where(
                    AgentEvent.agent_id == agent.id,
                    AgentEvent.kind == "log",
                    AgentEvent.at >= now - LOG_SPIKE_WINDOW,
                )
                .group_by(AgentEvent.service)
            )
        ).all()
    )
    firing: dict[str, list[str]] = {}
    started: list[Signal] = []
    before: dict[str, list[str]] = dict(agent.firing or {})
    for s in body.states:
        resource = resources.get(s.name)
        if resource is None:
            continue
        now_firing = service_signals(s, int(counts.get(s.name, 0)), resource, now)
        firing[s.name] = sorted({x.rule for x in now_firing})
        started += [x for x in now_firing if x.rule not in before.get(s.name, [])]
    if body.states:
        agent.firing = firing
    await session.commit()
    return started if settings.incident_watch_enabled else []


async def open_incidents(
    session: AsyncSession, workspace_id: uuid.UUID, signals: list[Signal]
) -> None:
    """One incident per service whose rules started firing (the store joins an open one)."""
    from app.incidents.store import open_incident

    by_resource: dict[uuid.UUID, list[Signal]] = {}
    for s in signals:
        if s.resource_id is not None:
            by_resource.setdefault(s.resource_id, []).append(s)
    for resource_id, found in by_resource.items():
        try:
            await open_incident(
                session,
                workspace_id=workspace_id,
                resource_ids=[resource_id],
                detected_by="watcher",
                signals=found,
            )
        except Exception:  # an incident that cannot open must not lose the agent's report
            logger.exception("agent signals: could not open an incident")
            await session.rollback()
