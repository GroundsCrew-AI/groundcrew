"""Emergency actions on services a Groundcrew agent watches (connection kind `agent`).

- agent_restart_service: the agent restarts the container (Docker, Compose) or unit (systemd).

A plan needs an enrolled, online agent that reports the service, and the service must `allow`
restart in the agent's own configuration file: Groundcrew cannot widen what the host allows.
`current` is the service's state as the agent last reported it ("hardhat-backend exited (exit
code 0)"); `execute` consumes the approval first, refuses if that state moved since the plan,
then queues a command signed with Groundcrew's command key (app/remote_agents/commands.py) and
waits for the agent's result (it long-polls for commands, so this takes seconds).
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.actions.base import (
    AGENT_RESTART_SERVICE,
    ActionPlan,
    ActionResult,
    EmergencyActionError,
)
from app.config import Settings, get_settings
from app.db.models import Agent, AgentCommand, Connection
from app.db.session import SessionFactory
from app.policy.actions import ApprovedAction, consume_approved_action
from app.remote_agents import commands
from app.remote_agents import inventory as inv
from app.remote_agents.signals import ServiceState, describe

# How long execute waits for the agent to report the command's result.
RESULT_TIMEOUT = timedelta(seconds=120)
POLL_SECONDS = 1.0


class AgentEmergencyActions:
    def __init__(self, session_factory: SessionFactory, settings: Settings | None = None) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()

    async def _load(self, connection_id: uuid.UUID) -> tuple[Connection, Agent]:
        async with self.session_factory() as session:
            conn = await session.get(Connection, connection_id)
            agent = await session.scalar(select(Agent).where(Agent.connection_id == connection_id))
        if conn is None or conn.kind != "agent" or agent is None or agent.public_key is None:
            raise EmergencyActionError("This connection has no enrolled Groundcrew agent.")
        return conn, agent

    def _online(self, agent: Agent, now: datetime) -> bool:
        offline_after = timedelta(seconds=self.settings.agent_offline_after_seconds)
        return agent.last_seen_at is not None and now - agent.last_seen_at < offline_after

    @staticmethod
    def _state(agent: Agent, service: str) -> ServiceState | None:
        raw = (agent.states or {}).get(service)
        return ServiceState.model_validate(raw) if raw else None

    async def plan_agent_restart_service(
        self, connection_id: uuid.UUID, service: str
    ) -> ActionPlan:
        conn, agent = await self._load(connection_id)
        host = agent.hostname or conn.name
        now = datetime.now(UTC)
        if not self._online(agent, now):
            raise EmergencyActionError(
                f"The agent on {host} is offline, so it cannot restart {service}."
            )
        stored = inv.stored(conn)
        facts = next((s for s in (stored.services if stored else []) if s.name == service), None)
        if facts is None:
            raise EmergencyActionError(f"The agent on {host} does not report {service}.")
        if commands.RESTART not in facts.allow:
            raise EmergencyActionError(
                f"{service} does not allow restart on {host}: add `allow: [restart]` to it in "
                "/etc/groundcrew-agent/agent.yaml, restart the agent, then run Medic again."
            )
        state = self._state(agent, service)
        if state is None or not state.found:
            raise EmergencyActionError(f"The agent on {host} cannot find {service} now.")
        current = describe(state)
        return ActionPlan(
            action=AGENT_RESTART_SERVICE,
            connection_id=connection_id,
            target=f"{host}/{service}",
            params={"service": service},
            current=current,
            desired=None,
            summary=f"Restart {service} on {host} (now {current.split(' ', 1)[-1]})",
            planned_at=now,
        )

    async def execute(
        self, plan: ActionPlan, token: ApprovedAction, *, run_id: uuid.UUID
    ) -> ActionResult:
        started = datetime.now(UTC)
        await consume_approved_action(
            self.session_factory,
            token,
            run_id=run_id,
            action=plan.action,
            plan_hash=plan.plan_hash(),
            settings=self.settings,
        )

        def result(ok: bool, summary: str, **kw: Any) -> ActionResult:
            return ActionResult(
                action=plan.action,
                ok=ok,
                summary=summary,
                started_at=started,
                finished_at=datetime.now(UTC),
                **kw,
            )

        service = plan.params["service"]
        try:
            conn, agent = await self._load(plan.connection_id)
        except EmergencyActionError as exc:
            return result(False, "Not run.", error=str(exc))
        if not self._online(agent, datetime.now(UTC)):
            return result(False, "Not run: the agent is offline.", error="no heartbeat")
        state = self._state(agent, service)
        now_state = describe(state) if state else f"{service} not reported"
        if now_state != plan.current:
            return result(
                False,
                f"Not run: {service} changed since the plan was approved.",
                error=f"it is {now_state}, the plan expected {plan.current}",
            )
        async with self.session_factory() as session:
            command = commands.issue(
                agent,
                action=commands.RESTART,
                service=service,
                plan_hash=plan.plan_hash(),
                run_id=run_id,
                settings=self.settings,
            )
            session.add(command)
            await session.commit()
            command_id = command.id
        deadline = datetime.now(UTC) + RESULT_TIMEOUT
        while datetime.now(UTC) < deadline:
            await asyncio.sleep(POLL_SECONDS)
            async with self.session_factory() as session:
                found = await session.get(AgentCommand, command_id)
                if found is None:
                    return result(False, "Not run: the command was removed.", error="removed")
                if found.status in ("succeeded", "failed", "expired"):
                    ok = found.status == "succeeded"
                    return result(
                        ok,
                        f"Restarted {service} on {agent.hostname or conn.name}."
                        if ok
                        else f"The agent could not restart {service}.",
                        details={"command_id": str(command_id)},
                        error=None if ok else (found.result or found.status),
                    )
        async with self.session_factory() as session:
            found = await session.get(AgentCommand, command_id, with_for_update=True)
            if found is not None and found.status in ("queued", "sent"):
                found.status, found.finished_at = "expired", datetime.now(UTC)
                found.result = "No result from the agent in time."
                await session.commit()
        return result(
            False,
            f"No result from the agent on {agent.hostname or conn.name} in time.",
            details={"command_id": str(command_id)},
            error="timed out waiting for the agent",
        )
