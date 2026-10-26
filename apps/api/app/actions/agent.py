"""Emergency actions on services a Groundcrew agent watches (connection kind `agent`).

- agent_restart_service: the agent restarts the container (Docker, Compose) or unit (systemd).
- agent_rollback_service: the agent recreates the container from the image it ran before (still
  on the host), with the same settings and volumes, and puts the old one back if the new one does
  not run and pass its health check. `current` / `desired` are "<image ref>@<short id>".

A plan needs an enrolled, online agent that reports the service, and the service must `allow`
restart in the agent's own configuration file: Groundcrew cannot widen what the host allows.
`current` is the service's state as the agent last reported it ("hardhat-backend exited (exit
code 0)"); `execute` consumes the approval first, refuses if that state moved since the plan,
then queues a command signed with Groundcrew's command key (app/remote_agents/commands.py) and
waits for the agent's result (it long-polls for commands, so this takes seconds).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.actions.base import (
    AGENT_RESTART_SERVICE,
    AGENT_ROLLBACK_SERVICE,
    ActionPlan,
    ActionResult,
    EmergencyActionError,
)
from app.config import Settings, get_settings
from app.db.models import Agent, Connection
from app.db.session import SessionFactory
from app.policy.actions import ApprovedAction, consume_approved_action
from app.remote_agents import commands
from app.remote_agents import inventory as inv
from app.remote_agents.signals import RUNNING, ServiceState, describe, image_label

# How long execute waits for the agent to report the command's result. On ECS and Kubernetes
# the agent waits for the rollout (apps/agent/internal/runtime: ECS 10 minutes, Kubernetes 5,
# and a rollback that fails waits again to put the old one back), so Groundcrew waits longer.
RESULT_TIMEOUT = timedelta(seconds=120)
ROLLOUT_TIMEOUTS = {"ecs": timedelta(minutes=11), "kubernetes": timedelta(minutes=6)}


def result_timeout(runtime: str | None, rollback: bool) -> timedelta:
    rollout = ROLLOUT_TIMEOUTS.get(runtime or "")
    if rollout is None:
        return RESULT_TIMEOUT
    return rollout * 2 if rollback else rollout


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

    async def _plan_base(
        self, connection_id: uuid.UUID, service: str, verb: str, allow: str
    ) -> tuple[Connection, Agent, str, ServiceState]:
        """The checks every agent action shares: an online agent that reports the service, and
        the service's `allow` in the agent's own configuration."""
        conn, agent = await self._load(connection_id)
        host = agent.hostname or conn.name
        if not self._online(agent, datetime.now(UTC)):
            raise EmergencyActionError(
                f"The agent on {host} is offline, so it cannot {verb} {service}."
            )
        stored = inv.stored(conn)
        facts = next((s for s in (stored.services if stored else []) if s.name == service), None)
        if facts is None:
            raise EmergencyActionError(f"The agent on {host} does not report {service}.")
        if allow not in facts.allow:
            raise EmergencyActionError(
                f"{service} does not allow {allow} on {host}: add {allow} to its `allow` list in "
                "/etc/groundcrew-agent/agent.yaml, restart the agent, then run Medic again."
            )
        state = self._state(agent, service)
        if state is None or not state.found:
            raise EmergencyActionError(f"The agent on {host} cannot find {service} now.")
        return conn, agent, inv.location(facts.runtime, facts.target, host), state

    async def plan_agent_restart_service(
        self, connection_id: uuid.UUID, service: str
    ) -> ActionPlan:
        _, _, host, state = await self._plan_base(
            connection_id, service, "restart", commands.RESTART
        )
        current = describe(state)
        return ActionPlan(
            action=AGENT_RESTART_SERVICE,
            connection_id=connection_id,
            target=f"{host}/{service}",
            params={"service": service},
            current=current,
            desired=None,
            summary=f"Restart {service} on {host} (now {current.split(' ', 1)[-1]})",
            planned_at=datetime.now(UTC),
        )

    async def plan_agent_rollback_service(
        self, connection_id: uuid.UUID, service: str
    ) -> ActionPlan:
        _, _, host, state = await self._plan_base(
            connection_id, service, "roll back", commands.ROLLBACK
        )
        if not state.image_id or not state.previous_image_id:
            raise EmergencyActionError(
                f"{service} on {host} has no earlier image to go back to (the agent remembers the "
                "images a container ran while it watches it; a Kubernetes Deployment's previous "
                "ReplicaSet or an ECS service's previous revision gives one)."
            )
        current = image_label(state.image, state.image_id)
        desired = image_label(state.previous_image, state.previous_image_id)
        return ActionPlan(
            action=AGENT_ROLLBACK_SERVICE,
            connection_id=connection_id,
            target=f"{host}/{service}",
            params={
                "service": service,
                "image": state.previous_image_id,
                "ref": state.previous_image or "",
                "from": state.image_id,
            },
            current=current,
            desired=desired,
            summary=f"Recreate {service} on {host} from {desired} (now {current})",
            planned_at=datetime.now(UTC),
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
        if plan.action == AGENT_ROLLBACK_SERVICE:
            now_state = image_label(state.image, state.image_id) if state else "not reported"
        else:
            now_state = describe(state) if state else f"{service} not reported"
        if now_state != plan.current:
            restart = plan.action != AGENT_ROLLBACK_SERVICE
            if restart and state and state.found and state.state in RUNNING:
                # It came back before the approval (e.g. ECS replaced the stopped task): the
                # restart is not needed, and Medic's checks decide whether it is healthy.
                return result(
                    True,
                    f"Not needed: {service} runs again; nothing was sent to the agent.",
                    details={"skipped": now_state},
                )
            return result(
                False,
                f"Not run: {service} changed since the plan was approved.",
                error=f"now {now_state}; the plan expected {plan.current}",
            )
        stored = inv.stored(conn)
        facts = next((s for s in (stored.services if stored else []) if s.name == service), None)
        host = agent.hostname or conn.name
        if facts is not None:
            host = inv.location(facts.runtime, facts.target, host)
        rollback = plan.action == AGENT_ROLLBACK_SERVICE
        async with self.session_factory() as session:
            command = commands.issue(
                agent,
                action=commands.ROLLBACK if rollback else commands.RESTART,
                params={"image": plan.params["image"], "ref": plan.params.get("ref", "")}
                if rollback
                else None,
                service=service,
                plan_hash=plan.plan_hash(),
                run_id=run_id,
                settings=self.settings,
            )
            session.add(command)
            await session.commit()
            command_id = command.id
        found = await commands.wait_for_result(
            self.session_factory,
            command_id,
            result_timeout(facts.runtime if facts else None, rollback),
            POLL_SECONDS,
        )
        if found is None:
            return result(False, "Not run: the command was removed.", error="removed")
        if found.status == "expired":
            return result(
                False,
                f"No result from the agent on {host} in time.",
                details={"command_id": str(command_id)},
                error="timed out waiting for the agent",
            )
        ok = found.status == "succeeded"
        done = (
            f"Rolled {service} on {host} back to {plan.desired}."
            if plan.action == AGENT_ROLLBACK_SERVICE
            else f"Restarted {service} on {host}."
        )
        return result(
            ok,
            done if ok else f"The agent could not {_verb(plan)} {service}.",
            details={"command_id": str(command_id)},
            error=None if ok else (found.result or found.status),
        )


def _verb(plan: ActionPlan) -> str:
    return "roll back" if plan.action == AGENT_ROLLBACK_SERVICE else "restart"
