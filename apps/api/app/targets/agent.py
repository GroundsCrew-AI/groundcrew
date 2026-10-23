"""Deploy target: a server with a Groundcrew agent (connection kind `agent`).

The deploy updates one service from the agent's configuration file, which must `allow`
deploy. Where the image goes:

- the Groundcrew registry (default; app/registry.py): the worker uploads the tested image to the
  registry on the platform's internal network, and the agent pulls it from
  REGISTRY_PUBLIC_HOST with a 30-minute pull token for this workspace's images;
- the connection's own registry (`connection.config["deploy"]`, the same settings as a
  Kubernetes cluster's: host/path with a username and token, or Amazon ECR through an AWS
  connection with the deploy capability): the build service pushes there, and the agent pulls
  with the same login, fetched fresh for the deploy (an ECR login lasts 12 hours).

Apply: the push, then a signed `deploy` command. The registry login and the env values are not
in the command's payload: they are stored encrypted on it and the agent fetches them once. The
agent pulls the image and recreates the container from it (keeping its other settings and
volumes; a missing container is created with the app's port published), checks it runs and
answers its health check, and otherwise puts the previous container back.

Verify reads the agent's next report (running, no failed health checks). Rollback recreates
the container from the image it ran before (the `rollback` command).
"""

import asyncio
import contextlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app import registry
from app.config import Settings
from app.connectors.credentials import CredentialsProvider
from app.connectors.deploy_infra import aws_tags, ecr_repository
from app.db.models import Agent, AgentEvent, Connection
from app.db.session import SessionFactory
from app.discovery.types import ConnectorResult
from app.pipeline.events import PreflightCheck, TargetKind, VerifyCheck
from app.policy.actions import (
    TARGET_APPLY,
    TARGET_ROLLBACK,
    ApprovedAction,
    consume_approved_action,
)
from app.remote_agents import commands
from app.remote_agents import inventory as inv
from app.remote_agents.signals import RUNNING, ServiceState, describe
from app.sandbox.base import PushTarget, RegistryLogin, Sandbox, SandboxError
from app.sandbox.registry import ecr_login
from app.targets.base import (
    ApplyStep,
    Deployment,
    DeploySpec,
    Health,
    LogLine,
    TargetError,
    TargetPlan,
    check_spec_matches,
    common_checks,
    env_views,
    plain_values,
    secret_values,
)
from app.targets.k8s_deploy import K8sDeploySettings

DEPLOY_TIMEOUT = timedelta(minutes=10)
VERIFY_WAIT = timedelta(seconds=60)
_APP = re.compile(r"[^a-z0-9._-]+")


def own_registry(conn: Connection) -> K8sDeploySettings | None:
    """The agent connection's own registry, or None for the Groundcrew registry."""
    return K8sDeploySettings.from_config(conn.config) if conn.kind == "agent" else None


def deployable_services(conn: Connection) -> list[str]:
    """The services the agent's configuration lets Groundcrew deploy to."""
    stored = inv.stored(conn)
    return [s.name for s in (stored.services if stored else []) if commands.DEPLOY in s.allow]


_PUBLISHED = re.compile(r"published on host port (\d+)")


def published_port(result: str) -> int | None:
    """The host port a new container was published on, from the agent's result (0.5.0 and
    later move to the next free port when the app's own port is in use)."""
    match = _PUBLISHED.search(result)
    return int(match.group(1)) if match else None


class AgentTarget:
    kind: TargetKind = "agent"

    def __init__(
        self,
        settings: Settings,
        credentials: CredentialsProvider,
        session_factory: SessionFactory,
        conn: Connection,
    ) -> None:
        self.settings = settings
        self.credentials = credentials
        self.session_factory = session_factory
        self.connection_id = conn.id
        self.conn = conn
        self.own = own_registry(conn)

    # --- helpers ---------------------------------------------------------------------------

    @staticmethod
    def _app(app: str) -> str:
        return _APP.sub("-", app.lower()).strip("-.") or "app"

    def _repository(self, app: str) -> str:
        if self.own is None:
            return registry.repository(self.conn.workspace_id, app)
        return ecr_repository(self.settings, app) if self.own.uses_ecr else app

    def _image(self, app: str, tag: str) -> str:
        if self.own is None:
            if not registry.enabled(self.settings):
                raise TargetError(
                    "The Groundcrew registry is not set up here: add your own registry on the "
                    "agent connection (Connections, Registry)."
                )
            return registry.public_image(self.settings, self.conn.workspace_id, app, tag)
        return self.own.image(self._repository(app), tag)

    async def _agent(self) -> Agent:
        async with self.session_factory() as session:
            agent = await session.scalar(
                select(Agent).where(Agent.connection_id == self.connection_id)
            )
        if agent is None or agent.public_key is None:
            raise TargetError(f"{self.conn.name} has no enrolled Groundcrew agent.")
        return agent

    def _online(self, agent: Agent) -> bool:
        last = agent.last_seen_at
        return last is not None and datetime.now(UTC) - last < timedelta(
            seconds=self.settings.agent_offline_after_seconds
        )

    @staticmethod
    def _state(agent: Agent, service: str) -> ServiceState | None:
        raw = (agent.states or {}).get(service)
        return ServiceState.model_validate(raw) if raw else None

    async def _ecr_login(self) -> RegistryLogin:
        own = self.own
        if own is None or own.aws_connection_id is None:
            raise TargetError("This agent's registry is not Amazon ECR.")
        session = self.credentials.aws_deploy_session(uuid.UUID(own.aws_connection_id))
        try:
            return await ecr_login(session, region=own.ecr_region)
        except Exception as exc:
            raise TargetError(
                f"Logging in to ECR through the AWS connection failed: {exc}"
            ) from None

    async def _ensure_ecr_repository(self, name: str, run_id: uuid.UUID) -> None:
        """ECR needs the repository before the push (the deploy role may create groundcrew-*)."""
        own = self.own
        if own is None or own.aws_connection_id is None:
            return
        session = self.credentials.aws_deploy_session(uuid.UUID(own.aws_connection_id))

        def create() -> None:
            client: Any = session.client("ecr", region_name=own.ecr_region)
            with contextlib.suppress(client.exceptions.RepositoryAlreadyExistsException):
                client.create_repository(repositoryName=name, tags=aws_tags(run_id))

        try:
            await asyncio.to_thread(create)
        except Exception as exc:
            raise TargetError(f"Creating the ECR repository {name} failed: {exc}") from None

    async def _pull_login(self) -> dict[str, str] | None:
        """What the agent pulls with: a Groundcrew pull token, or the own registry's login."""
        if self.own is None:
            return {
                "server": str(self.settings.registry_public_host),
                "username": registry.USERNAME,
                "password": registry.pull_token(self.settings, self.conn.workspace_id),
            }
        login = await self._ecr_login() if self.own.uses_ecr else self.own.login()
        if login is None:
            return None  # a public repository
        return {
            "server": login.registry,
            "username": login.username,
            "password": login.password.get_secret_value(),
        }

    async def _send(
        self,
        agent: Agent,
        action: str,
        service: str,
        plan: TargetPlan,
        params: dict[str, str],
        secrets: dict[str, Any] | None,
    ) -> str:
        """Queue a signed command and wait for the agent's result; raises on failure."""
        async with self.session_factory() as session:
            command = commands.issue(
                agent,
                action=action,
                service=service,
                plan_hash=plan.plan_hash(),
                run_id=plan.run_id,
                settings=self.settings,
                params=params,
                secrets=secrets,
            )
            session.add(command)
            await session.commit()
            command_id = command.id
        found = await commands.wait_for_result(self.session_factory, command_id, DEPLOY_TIMEOUT)
        if found is None:
            raise TargetError("The command was removed before the agent ran it.")
        if found.status != "succeeded":
            raise TargetError(
                f"The agent on {agent.hostname or self.conn.name} could not {action} "
                f"{service}: {found.result or found.status}"
            )
        return found.result or "done"

    # --- Target ----------------------------------------------------------------------------

    async def discover(self) -> ConnectorResult:
        # The agent's inventory reaches the graph through discovery of the connection.
        return ConnectorResult()

    async def plan(self, spec: DeploySpec) -> TargetPlan:
        service = spec.target_service
        if not service:
            raise TargetError("Choose which service on the server this deploy updates.")
        app = self._app(spec.app)
        image = self._image(app, spec.tag)
        agent = await self._agent()
        state = self._state(agent, service)
        host = agent.hostname or self.conn.name
        exists = state is not None and state.found
        where = "Groundcrew registry" if self.own is None else self.own.registry
        steps = [
            ApplyStep(kind="image", name=image, action="push", detail=f"to the {where}"),
            ApplyStep(
                kind="container",
                name=f"{host}/{service}",
                action="update" if exists else "create",
                detail="the agent pulls the image and recreates the container from it, keeping "
                "its settings and volumes; it goes back to the current one if the new one is not "
                "healthy"
                if exists
                else f"the agent creates the container and publishes port {spec.port} on the "
                f"host, or the next free port above it if {spec.port} is in use",
            ),
        ]
        return TargetPlan(
            target="agent",
            connection_id=spec.connection_id,
            run_id=spec.run_id,
            app=app,
            image=image,
            source_image_id=spec.source_image_id,
            image_tested=spec.smoke_passed,
            port=spec.port,
            health_path=spec.health_path,
            replicas=1,
            cpu="-",
            memory="-",
            env=env_views(spec),
            allow_missing_env=spec.allow_missing_env,
            steps=steps,
            rendered=json.dumps(
                {
                    "host": host,
                    "service": service,
                    "image": image,
                    "port": spec.port,
                    "health_path": spec.health_path,
                    "registry": where,
                },
                indent=2,
            ),
            rendered_format="json",
            previous_revision=state.image_id if state and state.image_id else None,
        )

    async def preflight(self, plan: TargetPlan) -> list[PreflightCheck]:
        checks = common_checks(plan)
        service = json.loads(plan.rendered).get("service", "")
        try:
            agent = await self._agent()
        except TargetError as exc:
            return [*checks, PreflightCheck(name="other", status="fail", detail=str(exc))]
        host = agent.hostname or self.conn.name
        if not self._online(agent):
            checks.append(
                PreflightCheck(
                    name="other", status="fail", detail=f"The agent on {host} is offline."
                )
            )
        if service not in deployable_services(self.conn):
            checks.append(
                PreflightCheck(
                    name="other",
                    status="fail",
                    detail=f"{service} does not allow deploy on {host}: add deploy to its `allow` "
                    "list in /etc/groundcrew-agent/agent.yaml and restart the agent.",
                )
            )
        state = self._state(agent, service)
        if state is not None and state.found and state.state not in RUNNING:
            checks.append(
                PreflightCheck(
                    name="other",
                    status="warn",
                    detail=f"{describe(state)} on {host} before the deploy",
                )
            )
        return checks

    async def apply(
        self, plan: TargetPlan, spec: DeploySpec, token: ApprovedAction, sandbox: Sandbox
    ) -> Deployment:
        await consume_approved_action(
            self.session_factory,
            token,
            run_id=plan.run_id,
            action=TARGET_APPLY,
            plan_hash=plan.plan_hash(),
        )
        check_spec_matches(plan, spec)
        service = str(spec.target_service)
        agent = await self._agent()
        repo, tag = self._repository(plan.app), spec.tag
        try:
            # 1. The push: nothing left the sandbox before approval.
            if self.own is None:
                await sandbox.push_to_groundcrew(
                    spec.source_image, str(self.settings.registry_internal_url), repo, tag
                )
            else:
                if self.own.uses_ecr:
                    await self._ensure_ecr_repository(repo, plan.run_id)
                login = await self._ecr_login() if self.own.uses_ecr else self.own.login()
                await sandbox.push(spec.source_image, PushTarget(image=plan.image, login=login))
        except SandboxError as exc:
            raise TargetError(f"Pushing the image failed: {exc}") from None
        # 2. The agent pulls it and recreates the container.
        env = [
            {"name": k, "value": v}
            for k, v in {**plain_values(spec), **secret_values(spec)}.items()
        ]
        result = await self._send(
            agent,
            commands.DEPLOY,
            service,
            plan,
            params={
                "image": plan.image,
                "port": str(plan.port),
                "health_path": plan.health_path or "",
            },
            secrets={"registry": await self._pull_login(), "env": env},
        )
        return Deployment(
            target="agent",
            connection_id=plan.connection_id,
            run_id=plan.run_id,
            app=plan.app,
            image=plan.image,
            revision=plan.image,
            previous_revision=plan.previous_revision,
            location=f"{agent.hostname or self.conn.name}/{service}",
            port=published_port(result) or plan.port,
            health_path=plan.health_path,
            external_id=inv.service_id(self.conn, service),
        )

    async def verify(self, deployment: Deployment) -> Health:
        """The agent's report after the deploy: running and passing its health checks."""
        service = deployment.location.split("/", 1)[-1]
        started = datetime.now(UTC)
        deadline = started + VERIFY_WAIT
        while True:
            agent = await self._agent()
            state = self._state(agent, service)
            fresh = agent.states_at is not None and agent.states_at > deployment.at
            if fresh and state is not None:
                running = state.found and state.state in RUNNING
                healthy = running and state.health_failures == 0
                if healthy or datetime.now(UTC) >= deadline:
                    detail = describe(state)
                    if state.health_failures:
                        detail += (
                            f", {state.health_failures} failed health checks "
                            f"({state.health_detail})"
                        )
                    return Health(
                        ok=healthy,
                        checks=[
                            VerifyCheck(name="rollout", ok=running, detail=detail),
                            VerifyCheck(
                                name="health",
                                ok=healthy,
                                detail="health checks passing" if healthy else detail,
                            ),
                        ],
                        duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
                    )
            if datetime.now(UTC) >= deadline:
                return Health(
                    ok=False,
                    checks=[
                        VerifyCheck(
                            name="rollout",
                            ok=False,
                            detail="no report from the agent since the deploy",
                        )
                    ],
                    duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
                )
            await asyncio.sleep(3)

    async def logs(self, deployment: Deployment, since: datetime) -> list[LogLine]:
        service = deployment.location.split("/", 1)[-1]
        agent = await self._agent()
        async with self.session_factory() as session:
            rows = await session.scalars(
                select(AgentEvent)
                .where(
                    AgentEvent.agent_id == agent.id,
                    AgentEvent.service == service,
                    AgentEvent.at >= since,
                )
                .order_by(AgentEvent.at)
                .limit(200)
            )
            return [LogLine(at=e.at, source=e.kind, message=e.message) for e in rows]

    async def rollback_plan(self, deployment: Deployment) -> TargetPlan:
        to = deployment.previous_revision
        step = ApplyStep(
            kind="container",
            name=deployment.location,
            action="update",
            detail=f"recreate from the image it ran before ({(to or '-')[:19]})"
            if to
            else "nothing ran there before this deploy: the container is left as it is",
        )
        return TargetPlan(
            action="rollback",
            target="agent",
            connection_id=deployment.connection_id,
            run_id=deployment.run_id,
            app=deployment.app,
            image=deployment.image,
            source_image_id=None,
            image_tested=True,
            port=deployment.port,
            health_path=deployment.health_path,
            replicas=1,
            cpu="-",
            memory="-",
            env=[],
            allow_missing_env=False,
            steps=[step],
            rendered=json.dumps(
                {"location": deployment.location, "from": deployment.revision, "to": to}, indent=2
            ),
            rendered_format="json",
            previous_revision=deployment.revision,
            rollback_to_revision=to,
        )

    async def rollback(self, plan: TargetPlan, token: ApprovedAction) -> Deployment:
        await consume_approved_action(
            self.session_factory,
            token,
            run_id=plan.run_id,
            action=TARGET_ROLLBACK,
            plan_hash=plan.plan_hash(),
        )
        location = json.loads(plan.rendered)["location"]
        service = location.split("/", 1)[-1]
        agent = await self._agent()
        if plan.rollback_to_revision:
            # The image's name, when the agent still reports it, so the container shows it.
            state = self._state(agent, service)
            ref = (
                state.previous_image
                if state and state.previous_image_id == plan.rollback_to_revision
                else None
            )
            await self._send(
                agent,
                commands.ROLLBACK,
                service,
                plan,
                params={"image": plan.rollback_to_revision, "ref": ref or ""},
                secrets=None,
            )
        return Deployment(
            target="agent",
            connection_id=plan.connection_id,
            run_id=plan.run_id,
            app=plan.app,
            image=plan.rollback_to_revision or plan.image,
            revision=plan.rollback_to_revision,
            previous_revision=plan.previous_revision,
            location=location,
            port=plan.port,
            health_path=plan.health_path,
            status="rolled_back",
        )
