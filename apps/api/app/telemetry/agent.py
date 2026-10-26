"""Agent reader: what a Groundcrew agent reported about one service, from Groundcrew's own
database (nothing on the host is called). Lines: state changes, failed and recovered health
checks, restarts, error log lines (redacted on the host), commands it ran, and the service's
state now. An image change the agent saw is a `deploy` change marker (ref
"<host>/<service>@<digest>")."""

from datetime import datetime

from sqlalchemy import select

from app.agents.medic.events import ChangeMarker
from app.db.models import Agent, AgentEvent, Resource
from app.db.session import SessionFactory
from app.remote_agents import inventory as inv
from app.remote_agents.signals import ServiceState, describe
from app.telemetry.evidence import ReaderOutput, line

SOURCE = "agent"
MAX_EVENTS = 300
# What runs the service when it is not the agent's host: who starts replacements.
ORCHESTRATED = {
    "ecs": (
        "runs on Amazon ECS (service {target}): ECS starts replacement tasks itself; the "
        "Groundcrew agent only watches it and restarts it when someone approves"
    ),
    "kubernetes": (
        "runs on Kubernetes (Deployment {target}): Kubernetes starts replacement pods itself; "
        "the Groundcrew agent only watches it and restarts it when someone approves"
    ),
}


async def read_service(
    session_factory: SessionFactory, resource: Resource, since: datetime, until: datetime
) -> ReaderOutput:
    service = resource.name
    async with session_factory() as session:
        agent = await session.scalar(
            select(Agent).where(Agent.connection_id == resource.connection_id)
        )
        if agent is None:
            return ReaderOutput(lines=[line(until, SOURCE, service, "no agent for this service")])
        rows = list(
            await session.scalars(
                select(AgentEvent)
                .where(
                    AgentEvent.agent_id == agent.id,
                    AgentEvent.service == service,
                    AgentEvent.at >= since,
                    AgentEvent.at <= until,
                )
                .order_by(AgentEvent.at.desc())
                .limit(MAX_EVENTS)
            )
        )
    cfg = resource.config or {}
    runtime, target = cfg.get("runtime"), cfg.get("target")
    host = inv.location(runtime, target, agent.hostname or resource.account or "host")
    out = ReaderOutput(truncated=len(rows) >= MAX_EVENTS)
    if runtime in ORCHESTRATED:
        out.lines.append(line(since, SOURCE, service, ORCHESTRATED[runtime].format(target=target)))
    for e in reversed(rows):
        text = e.message if e.kind == "log" else f"[{e.kind}] {e.message}"
        out.lines.append(line(e.at, SOURCE, service, text))
        if e.kind == "deploy":
            digest = e.message.rsplit(" to ", 1)[-1]
            out.changes.append(
                ChangeMarker(
                    kind="deploy",
                    ref=f"{host}/{service}@{digest}",
                    at=e.at,
                    resource_id=resource.id,
                    label=f"{service} on {host}: {e.message}"[:200],
                )
            )
    raw = (agent.states or {}).get(service)
    if raw:
        s = ServiceState.model_validate(raw)
        now = f"now: {describe(s)} on {host}, {s.restarts} restarts"
        if s.health_failures:
            now += f", {s.health_failures} health checks failed in a row ({s.health_detail})"
        out.lines.append(line(agent.states_at or until, SOURCE, service, now))
    return out
