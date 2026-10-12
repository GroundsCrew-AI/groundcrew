"""The deploy pipeline (Phase 4, M6): Scout -> Builder -> Deployer in one run (agent="deploy").

    POST /deploys -> run (queued) + job "deploy" {run_id, env: <Fernet>}
    worker: scout -> build -> smoke -> plan -> preflight -> approval   (pauses: waiting_approval)
    POST /approvals/{id}/decide -> job "resume_run" -> apply -> verify -> live URL
                                   (verify failed -> rollback offered -> emergency approval)
    POST /deploys/{id}/env -> job "resume_run" with state.replan: preflight again, same image

Everything a resumed job needs is in `runs.state` (PipelineState plus the keys below), so a
run pauses at an approval and resumes in a new job, also after a worker restart. The stages
each emit `stage_started` / `stage_finished`; Builder's attempts, the plan, verify, the live
URL, and the end are deploy events (docs/events.md).

Secret env values live only Fernet-encrypted in the "deploy" job's payload. They are never
written to runs.state, approvals, steps, or logs, and are purged from the job when the run
ends. Nothing leaves the sandbox before approval (the push is the first apply step) and
apply/rollback need a single-use ApprovedAction (app.policy.actions).
"""

import asyncio
import contextlib
import json
import logging
import shutil
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.builder import Builder, BuilderError
from app.agents.deployer import (
    DeployDraft,
    Deployer,
    blocked,
    deploy_plan_card,
    deploy_spec,
    plan_summary,
)
from app.agents.scout.appspec import AppSpec
from app.agents.scout.scout import scout
from app.config import Settings, get_settings
from app.connectors.credentials import ConnectionCredentialsProvider, CredentialsProvider
from app.connectors.deploy_infra import slug
from app.db.models import (
    TERMINAL_RUN_STATUSES,
    Approval,
    ApprovalStatus,
    Connection,
    Job,
    Resource,
    Run,
    RunStatus,
    StopReason,
    ToolPolicy,
)
from app.db.session import SessionFactory
from app.discovery.types import DiscoveredResource
from app.graph.sink import Deployment as SinkDeployment
from app.graph.sink import GraphResourceSink, ResourceSink
from app.llm.budget import BudgetExceededError, PgUsageStore, RunBudget
from app.llm.client import ChatModel, LLMResult, TokenFactory, describe_result
from app.pipeline.events import (
    BuildAttempt,
    DeployStage,
    LiveUrl,
    RollbackOffered,
    RunFinished,
    StageAgent,
    StageFinished,
    StageStarted,
    StageStatus,
    VerifyResult,
)
from app.pipeline.state import DeployRecord, DeployStatus, PipelineState, StageState
from app.policy.actions import TARGET_APPLY, TARGET_ROLLBACK, issue_approved_action
from app.repos.source import RepoSource
from app.sandbox import get_sandbox
from app.sandbox.base import Sandbox, SmokeEnv
from app.security.crypto import decrypt, encrypt
from app.security.redact import summarize
from app.steps import record_step
from app.targets import Target, TargetError, TargetPlan, target_for
from app.targets.base import Deployment as TargetDeployment

logger = logging.getLogger("groundcrew.pipeline")

DEPLOY_JOB = "deploy"
# demo/fixtures in a checkout (apps/api/app/pipeline/runner.py -> the repo root), or
# DEPLOY_FIXTURES_DIR (the worker container mounts them there in dev).
_HERE = Path(__file__).resolve()
CHECKOUT_FIXTURES = _HERE.parents[4] / "demo" / "fixtures" if len(_HERE.parents) > 4 else None


def fixtures_dir(settings: Settings | None = None) -> Path:
    configured = (settings or get_settings()).deploy_fixtures_dir
    if configured:
        return Path(configured)
    if CHECKOUT_FIXTURES is not None and CHECKOUT_FIXTURES.is_dir():
        return CHECKOUT_FIXTURES
    raise PipelineInputError("No demo fixtures here (set DEPLOY_FIXTURES_DIR).")


# Phases a run can be paused in (runs.state["phase"]).
START = "start"
AWAIT_PLAN = "awaiting_plan_approval"
AWAIT_ROLLBACK = "awaiting_rollback_approval"
DONE = "done"


class PipelineStopError(Exception):
    """The run was stopped (by a user) while the pipeline worked: exit quietly."""


class Finished(Exception):  # noqa: N818 (control flow, not an error)
    """The pipeline reached a terminal state for this job."""


# --- dependencies (replaceable in tests and in fake mode) ---------------------------------


class SourceFactory(Protocol):
    def __call__(self, request: dict[str, Any], conn: Connection | None) -> RepoSource: ...


TargetFactory = Callable[[Settings, CredentialsProvider, Connection], Target]
CredentialsFactory = Callable[[AsyncSession, uuid.UUID], Awaitable[CredentialsProvider]]


async def _workspace_credentials(
    session: AsyncSession, workspace_id: uuid.UUID
) -> CredentialsProvider:
    return await ConnectionCredentialsProvider.for_workspace(session, workspace_id)


class FixtureCopy:
    """A demo fixture copied to a temp dir, so Builder writes only to its own copy."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.commit: str | None = None
        self._dir: Path | None = None

    def materialize(self) -> Path:
        base = fixtures_dir()
        source = (base / self.name / "repo").resolve()
        if not source.is_relative_to(base.resolve()) or not source.is_dir():
            raise PipelineInputError(f"No fixture {self.name!r} in demo/fixtures.")
        self._dir = Path(tempfile.mkdtemp(prefix="gc-fixture-"))
        target = self._dir / "repo"
        shutil.copytree(source, target, symlinks=False)
        return target

    def describe(self) -> str:
        return f"fixture {self.name}"

    def cleanup(self) -> None:
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)


class PipelineInputError(RuntimeError):
    """The request cannot be deployed (missing fixture, connection gone). Shown to the user."""


def default_source(request: dict[str, Any], conn: Connection | None) -> RepoSource:
    source = request["source"]
    if source["kind"] == "fixture":
        return FixtureCopy(source["repo"])
    from app.connectors.github_source import GitHubAppSource

    if conn is None or conn.kind != "github" or not conn.config.get("installation_id"):
        raise PipelineInputError("The GitHub connection for this deploy is gone or not installed.")
    return GitHubAppSource(int(conn.config["installation_id"]), source["repo"], source.get("ref"))


@dataclass
class PipelineDeps:
    settings: Settings
    llm: ChatModel
    sandbox: Sandbox
    sink: ResourceSink
    source_for: SourceFactory = default_source
    target_for: TargetFactory = target_for
    credentials_for: CredentialsFactory = _workspace_credentials
    builder_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


def default_deps(session_factory: SessionFactory, settings: Settings | None = None) -> PipelineDeps:
    settings = settings or get_settings()
    return PipelineDeps(
        settings=settings,
        llm=TokenFactory(settings),
        sandbox=get_sandbox(settings),
        sink=GraphResourceSink(session_factory),
    )


# --- env values (job payload only) ---------------------------------------------------------


def seal_env(env: list[dict[str, Any]]) -> str:
    """[{name, value, secret}] -> Fernet token for the job payload."""
    return encrypt(json.dumps(env))


def open_env(token: str | None) -> dict[str, tuple[str, bool]]:
    if not token:
        return {}
    return {v["name"]: (v["value"], bool(v["secret"])) for v in json.loads(decrypt(token))}


async def deploy_job_row(session: AsyncSession, run_id: uuid.UUID) -> Job | None:
    """The run's "deploy" job: the only place its env values are (encrypted)."""
    return await session.scalar(
        select(Job)
        .where(Job.kind == DEPLOY_JOB, Job.payload["run_id"].astext == str(run_id))
        .order_by(Job.created_at.desc())
        .limit(1)
    )


async def purge_env(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Drop the env values from every job of this run once it ended."""
    jobs = (
        await session.scalars(select(Job).where(Job.payload["run_id"].astext == str(run_id)))
    ).all()
    for job in jobs:
        if isinstance(job.payload, dict) and "env" in job.payload:
            job.payload = {k: v for k, v in job.payload.items() if k != "env"}


# --- the runner ----------------------------------------------------------------------------


class _Recorder:
    """on_call hook: every model call is an "llm" step and adds to the run's totals; keeps the
    step's seq so Builder's attempt cards can point at their model call."""

    def __init__(self, session: AsyncSession, run: Run, agent: str) -> None:
        self.session = session
        self.run = run
        self.agent = agent
        self.last_seq: int | None = None

    async def __call__(self, result: LLMResult) -> None:
        run = self.run
        run.prompt_tokens += result.prompt_tokens
        run.completion_tokens += result.completion_tokens
        run.cost_usd = (run.cost_usd or Decimal(0)) + result.cost_usd
        run.ultra_equiv_usd = (run.ultra_equiv_usd or Decimal(0)) + result.ultra_equiv_usd
        step = await record_step(
            self.session,
            run,
            kind="llm",
            agent=self.agent,
            model_tier=result.tier,
            model_id=result.model_id,
            reasoning_mode=result.reasoning_mode,
            input_summary=f"{result.task}",
            output_summary=describe_result(result),
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            latency_ms=result.latency_ms,
            cost_usd=result.cost_usd,
            ultra_equiv_usd=result.ultra_equiv_usd,
            fell_back=result.fell_back,
            escalated=result.escalated,
            reasoning_excerpt=result.reasoning,
        )
        self.last_seq = step.seq
        await self.session.commit()


def _now() -> datetime:
    return datetime.now(UTC)


class PipelineRunner:
    def __init__(self, session_factory: SessionFactory, deps: PipelineDeps | None = None) -> None:
        self.session_factory = session_factory
        self._deps = deps

    @property
    def deps(self) -> PipelineDeps:
        if self._deps is None:
            self._deps = default_deps(self.session_factory)
        return self._deps

    # --- entry --------------------------------------------------------------------------

    async def run(self, run_id: uuid.UUID) -> None:
        async with self.session_factory() as session:
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None:
                logger.warning("deploy run not found", extra={"run_id": str(run_id)})
                return
            if run.status not in (RunStatus.queued, RunStatus.running):
                await session.rollback()
                return
            run.status = RunStatus.running
            run.stop_reason = None
            run.started_at = run.started_at or _now()
            await session.commit()
            job = _Job(self, session, run)
            try:
                await job.drive()
            except Finished:
                pass
            except PipelineStopError:
                await session.rollback()
                await job.cleanup(final=True)
            except BudgetExceededError as exc:
                await session.rollback()
                await session.refresh(run)
                await record_step(session, run, kind="budget", output_summary=str(exc))
                with contextlib.suppress(Finished):
                    await job.finish(RunStatus.stopped, StopReason.budget, error=str(exc))
            except Exception as exc:
                logger.exception("deploy run failed", extra={"run_id": str(run_id)})
                await session.rollback()
                await session.refresh(run)
                error = summarize(f"{type(exc).__name__}: {exc}", 1000)
                await record_step(session, run, kind="error", output_summary=error)
                with contextlib.suppress(Finished):
                    await job.finish(RunStatus.failed, StopReason.error, error=error)


class _Job:
    """One job's pass over a deploy run (from the start, or resumed after an approval)."""

    def __init__(self, runner: PipelineRunner, session: AsyncSession, run: Run) -> None:
        self.runner = runner
        self.deps = runner.deps
        self.settings = runner.deps.settings
        self.session = session
        self.run = run
        self.state: dict[str, Any] = dict(run.state or {})
        self.recorders: dict[str, _Recorder] = {}
        self._credentials: CredentialsProvider | None = None
        self._budget: RunBudget | None = None

    # --- state and events ----------------------------------------------------------------

    async def save(self) -> None:
        self.run.state = json.loads(json.dumps(self.state, default=str))
        await self.session.commit()

    def stages(self) -> dict[str, Any]:
        stages: dict[str, Any] = self.state.setdefault("stages", {})
        return stages

    async def event(self, agent: str, data: Any, **extra: Any) -> None:
        await record_step(self.session, self.run, kind=data.type, agent=agent, data=data, **extra)
        await self.session.commit()

    async def stage_start(self, stage: DeployStage, agent: StageAgent) -> float:
        self.stages()[stage] = StageState(status="running", started_at=_now()).model_dump(
            mode="json"
        )
        self.state["stage"] = stage
        await self.save()
        await self.event(agent, StageStarted(stage=stage, agent=agent))
        return time.monotonic()

    async def stage_end(
        self,
        stage: DeployStage,
        agent: StageAgent,
        started: float,
        status: StageStatus,
        summary: str | None,
    ) -> None:
        entry = self.stages().get(stage, {})
        entry.update(status=status, finished_at=_now().isoformat())
        self.stages()[stage] = entry
        await self.save()
        await self.event(
            agent,
            StageFinished(
                stage=stage,
                agent=agent,
                status=status,
                duration_ms=int((time.monotonic() - started) * 1000),
                summary=summary,
            ),
        )

    def recorder(self, agent: str) -> _Recorder:
        if agent not in self.recorders:
            self.recorders[agent] = _Recorder(self.session, self.run, agent)
        return self.recorders[agent]

    async def budget(self) -> RunBudget:
        if self._budget is None:
            self._budget = await RunBudget.load(
                self.session, self.run, PgUsageStore(self.runner.session_factory), self.settings
            )
        return self._budget

    async def check_not_stopped(self) -> None:
        status = await self.session.scalar(select(Run.status).where(Run.id == self.run.id))
        await self.session.commit()
        if status in TERMINAL_RUN_STATUSES:
            raise PipelineStopError

    @property
    def request(self) -> dict[str, Any]:
        return dict(self.state.get("request") or {})

    async def env_values(self) -> dict[str, tuple[str, bool]]:
        job = await deploy_job_row(self.session, self.run.id)
        await self.session.commit()
        token = (
            job.payload.get("env") if job is not None and isinstance(job.payload, dict) else None
        )
        return open_env(token)

    async def credentials(self) -> CredentialsProvider:
        if self._credentials is None:
            self._credentials = await self.deps.credentials_for(self.session, self.run.workspace_id)
            await self.session.commit()
        return self._credentials

    async def target(self) -> Target:
        conn_id = uuid.UUID(self.request["target_connection_id"])
        conn = await self.session.get(Connection, conn_id)
        await self.session.commit()
        if conn is None or conn.workspace_id != self.run.workspace_id:
            raise PipelineInputError("The target connection for this deploy is gone.")
        target = self.deps.target_for(self.settings, await self.credentials(), conn)
        if hasattr(target, "_session_factory"):
            target._session_factory = self.runner.session_factory
        return target

    # --- driving ---------------------------------------------------------------------------

    async def drive(self) -> None:
        try:
            phase = self.state.get("phase", START)
            if phase == START:
                await self.from_start()
            elif phase == AWAIT_PLAN:
                await self.after_plan_decision()
            elif phase == AWAIT_ROLLBACK:
                await self.after_rollback_decision()
        finally:
            if self._credentials is not None and hasattr(self._credentials, "close"):
                self._credentials.close()

    async def pause(self, phase: str) -> None:
        self.state["phase"] = phase
        await self.save()
        locked = await self.session.get(
            Run, self.run.id, with_for_update=True, populate_existing=True
        )
        if locked is None or locked.status in TERMINAL_RUN_STATUSES:
            await self.session.rollback()
            raise PipelineStopError
        locked.status = RunStatus.waiting_approval
        locked.stop_reason = StopReason.waiting_approval
        await self.session.commit()
        raise Finished

    async def finish(
        self,
        status: RunStatus,
        reason: StopReason,
        *,
        error: str | None = None,
        live_url: str | None = None,
        deploy_status: str | None = None,
    ) -> None:
        """Last event, final status, purge secrets, clean the sandbox."""
        self.state["phase"] = DONE
        build = self.state.get("build") or {}
        started = self.run.started_at or self.run.created_at
        duration = int((_now() - started).total_seconds() * 1000) if started else None
        self.state["final"] = {"deploy_status": deploy_status, "live_url": live_url}
        await self.save()
        await self.event(
            "deploy",
            RunFinished(
                status=status,
                stop_reason=reason,
                error=error,
                live_url=live_url,
                build_attempts=int(build.get("attempts", 0)),
                automatic_fix=bool(build.get("automatic_fix", False)),
                duration_ms=duration,
                cost_usd=float(self.run.cost_usd) if self.run.cost_usd is not None else None,
                ultra_equiv_usd=float(self.run.ultra_equiv_usd)
                if self.run.ultra_equiv_usd is not None
                else None,
            ),
        )
        current = await self.session.scalar(
            select(Run.status).where(Run.id == self.run.id).with_for_update()
        )
        if current not in TERMINAL_RUN_STATUSES:
            self.run.status = status
            self.run.stop_reason = reason
            self.run.error = error
            self.run.finished_at = _now()
        await purge_env(self.session, self.run.id)
        await self.session.commit()
        await self.cleanup(final=True)
        raise Finished

    async def cleanup(self, *, final: bool) -> None:
        if final:
            async with self.runner.session_factory() as session:
                await purge_env(session, self.run.id)
                await session.commit()
            try:
                await self.deps.sandbox.cleanup(str(self.run.id))
            except Exception:
                logger.warning("sandbox cleanup failed", exc_info=True)

    # --- start: scout, build, smoke, plan, preflight, approval --------------------------------

    async def from_start(self) -> None:
        request = self.request
        source_conn: Connection | None = None
        if request["source"].get("connection_id"):
            source_conn = await self.session.get(
                Connection, uuid.UUID(request["source"]["connection_id"])
            )
            await self.session.commit()
        source = self.deps.source_for(request, source_conn)
        try:
            await self.check_not_stopped()
            started = await self.stage_start("scout", "scout")
            try:
                root = await asyncio.to_thread(source.materialize)
            except Exception as exc:
                await self.stage_end(
                    "scout", "scout", started, "failed", "Could not read the repository"
                )
                await self.finish(
                    RunStatus.failed,
                    StopReason.error,
                    error=summarize(f"Could not read the repository: {exc}", 500),
                )
            commit = getattr(source, "commit", None)
            found = await scout(
                root, self.deps.llm, await self.budget(), on_call=self.recorder("scout")
            )
            spec = found.spec
            app = slug(request["source"]["repo"].rsplit("/", 1)[-1])
            self.state.update(spec=spec.model_dump(mode="json"), app=app, commit=commit)
            facts = [str(spec.framework or spec.language or "unknown app")]
            if spec.port:
                facts.append(f"port {spec.port}")
            if spec.health_path:
                facts.append(f"health {spec.health_path}")
            await self.stage_end("scout", "scout", started, "succeeded", ", ".join(facts))

            await self.check_not_stopped()
            outcome = await self.build(root, spec, app, commit)
        finally:
            await asyncio.to_thread(source.cleanup)

        await self.smoke(spec, outcome)
        await self.plan_and_ask(replan=False)

    async def build(self, root: Path, spec: AppSpec, app: str, commit: str | None) -> Any:
        started = await self.stage_start("build", "builder")
        tag = (commit or self.run.id.hex)[:12]
        builder = Builder(self.deps.llm, self.deps.sandbox, sleep=self.deps.builder_sleep)
        attempts: list[BuildAttempt] = []

        async def on_attempt(attempt: BuildAttempt) -> None:
            attempts.append(attempt)
            await self.event("builder", attempt)

        async def on_note(text: str) -> None:
            await record_step(
                self.session, self.run, kind="note", agent="builder", output_summary=text
            )
            await self.session.commit()

        try:
            outcome = await builder.build(
                root,
                spec,
                app=app,
                tag=tag,
                run_id=str(self.run.id),
                budget=await self.budget(),
                recorder=self.recorder("builder"),
                on_attempt=on_attempt,
                on_note=on_note,
            )
        except BuilderError as exc:
            await self.stage_end("build", "builder", started, "failed", str(exc)[:200])
            await self.finish(RunStatus.failed, StopReason.error, error=str(exc))
        self.state["build"] = {
            "attempts": len(outcome.attempts),
            "automatic_fix": outcome.automatic_fix,
            "image_ref": outcome.image_ref,
            "image_id": outcome.image_id,
            "port": outcome.port,
            "tag": tag,
        }
        if not outcome.ok:
            await self.stage_end("build", "builder", started, "failed", outcome.error)
            reason = (
                StopReason.no_progress if outcome.stop_reason == "no_progress" else StopReason.error
            )
            await self.finish(RunStatus.failed, reason, error=outcome.error)
        n = len(outcome.attempts)
        summary = (
            f"Built on attempt {n} after an automatic fix"
            if outcome.automatic_fix
            else f"Built on attempt {n}"
        )
        await self.stage_end("build", "builder", started, "succeeded", summary)
        return outcome

    async def smoke(self, spec: AppSpec, outcome: Any) -> None:
        await self.check_not_stopped()
        started = await self.stage_start("smoke", "builder")
        env = [
            SmokeEnv(name, _secret(value), secret)
            for name, (value, secret) in (await self.env_values()).items()
        ]
        result = await Builder(self.deps.llm, self.deps.sandbox).smoke(
            outcome, spec, env, run_id=str(self.run.id)
        )
        checks = ", ".join(f"{c.path} {c.status_code or c.detail}" for c in result.checks)
        self.state["build"]["smoke"] = {"ok": result.ok, "checks": checks}
        if not result.ok:
            await self.stage_end("smoke", "builder", started, "failed", checks or result.detail)
            logs = f"\n{result.logs_tail}" if result.logs_tail else ""
            await self.finish(
                RunStatus.failed,
                StopReason.error,
                error=summarize(f"The smoke test failed ({checks or result.detail}).{logs}", 1500),
            )
        await self.stage_end("smoke", "builder", started, "succeeded", f"{checks} in the sandbox")

    async def plan_and_ask(self, *, replan: bool) -> None:
        """plan (plan_deploy + render), preflight, and the approval card. On a replan (the env
        changed at the approval card) only preflight runs again, with the same image."""
        await self.check_not_stopped()
        spec = AppSpec.model_validate(self.state["spec"])
        build = self.state["build"]
        request = self.request
        env = await self.env_values()
        target = await self.target()
        if not replan:
            started = await self.stage_start("plan", "deployer")
            draft, note = await Deployer(self.deps.llm).draft(
                spec,
                [(name, secret) for name, (_, secret) in env.items()],
                target.kind,
                budget=await self.budget(),
                recorder=self.recorder("deployer"),
            )
            if note:
                await record_step(
                    self.session, self.run, kind="note", agent="deployer", output_summary=note
                )
            self.state["draft"] = draft.model_dump(mode="json")
        draft = DeployDraft.model_validate(self.state["draft"])
        deploy = deploy_spec(
            run_id=self.run.id,
            connection_id=uuid.UUID(request["target_connection_id"]),
            app=self.state["app"],
            spec=spec,
            draft=draft,
            env_values=env,
            source_image=build["image_ref"],
            source_image_id=build["image_id"],
            smoke_passed=bool((build.get("smoke") or {}).get("ok")),
            tag=build["tag"],
            port=int(build["port"]),
            allow_missing_env=bool(request.get("allow_missing_env")),
            repo=request["source"]["repo"],
            commit=self.state.get("commit"),
        )
        plan = await target.plan(deploy)
        if not replan:
            await self.stage_end(
                "plan",
                "deployer",
                started,
                "succeeded",
                f"{len(plan.steps)} apply steps on {_where(plan)}",
            )

        started = await self.stage_start("preflight", "deployer")
        checks = await target.preflight(plan)
        failing = blocked(checks)
        summary = (
            "Blocked: " + "; ".join(c.detail for c in failing)
            if failing
            else "Image built and smoke-tested; env, port, and names checked"
        )
        await self.stage_end(
            "preflight", "deployer", started, "failed" if failing else "succeeded", summary
        )

        self.state["target_plan"] = plan.model_dump(mode="json")
        card = deploy_plan_card(plan, checks)
        await self.stage_start("approval", "deployer")
        count = int(self.state.get("plan_approvals", 0)) + 1
        approval = Approval(
            run_id=self.run.id,
            workspace_id=self.run.workspace_id,
            tool_call_id=f"deploy-plan-{count}",
            tool_name=TARGET_APPLY,
            policy=ToolPolicy.write,
            arguments={
                "plan_hash": plan.plan_hash(),
                "app": plan.app,
                "target": plan.target,
                "image": plan.image,
            },
            status=ApprovalStatus.pending,
        )
        self.session.add(approval)
        await self.session.flush()
        self.state["plan_approvals"] = count
        self.state["plan_approval_id"] = str(approval.id)
        self.state["replan"] = False
        self.stages()["approval"]["approval_id"] = str(approval.id)
        self.stages()["approval"]["status"] = "waiting_approval"
        await record_step(
            self.session,
            self.run,
            kind="approval_requested",
            agent="deployer",
            tool_name=TARGET_APPLY,
            approval_id=approval.id,
            output_summary=f"Waiting for approval to deploy {plan.app}",
            data=card,
        )
        await self.pause(AWAIT_PLAN)

    # --- after the plan decision: apply and verify ------------------------------------------

    async def after_plan_decision(self) -> None:
        if self.state.get("replan"):
            await self.plan_and_ask(replan=True)
        approval = await self.session.get(Approval, uuid.UUID(self.state["plan_approval_id"]))
        await self.session.commit()
        if approval is None or approval.status == ApprovalStatus.pending:
            await self.pause(AWAIT_PLAN)
            return
        started_at = self.stages().get("approval", {}).get("started_at")
        started = time.monotonic() - _seconds_since(started_at)
        if approval.status == ApprovalStatus.rejected:
            await self.stage_end(
                "approval", "deployer", started, "failed", "Plan rejected; nothing was changed"
            )
            await self.finish(
                RunStatus.stopped,
                StopReason.stopped_by_user,
                error="The plan was rejected. Nothing was deployed.",
            )
        who = approval.decided_via or "a workspace member"
        await self.stage_end("approval", "deployer", started, "succeeded", f"Plan approved ({who})")
        await self.apply_and_verify(approval)

    def _spec_for_apply(self, plan: TargetPlan, env: dict[str, tuple[str, bool]]) -> Any:
        spec = AppSpec.model_validate(self.state["spec"])
        build = self.state["build"]
        request = self.request
        return deploy_spec(
            run_id=self.run.id,
            connection_id=plan.connection_id,
            app=self.state["app"],
            spec=spec,
            draft=DeployDraft.model_validate(self.state["draft"]),
            env_values=env,
            source_image=build["image_ref"],
            source_image_id=build["image_id"],
            smoke_passed=bool((build.get("smoke") or {}).get("ok")),
            tag=build["tag"],
            port=int(build["port"]),
            allow_missing_env=bool(request.get("allow_missing_env")),
            repo=request["source"]["repo"],
            commit=self.state.get("commit"),
        )

    async def apply_and_verify(self, approval: Approval) -> None:
        await self.check_not_stopped()
        plan = TargetPlan.model_validate(self.state["target_plan"])
        target = await self.target()
        env = await self.env_values()
        token = await issue_approved_action(
            self.session,
            approval,
            run_id=self.run.id,
            action=TARGET_APPLY,
            plan_hash=plan.plan_hash(),
        )
        await self.session.commit()
        started = await self.stage_start("apply", "deployer")
        try:
            deployment = await target.apply(
                plan, self._spec_for_apply(plan, env), token, self.deps.sandbox
            )
        except TargetError as exc:
            await self.stage_end("apply", "deployer", started, "failed", str(exc)[:200])
            await self.finish(
                RunStatus.failed, StopReason.error, error=str(exc), deploy_status="failed"
            )
        self.state["deployment"] = deployment.model_dump(mode="json")
        await self.stage_end(
            "apply",
            "deployer",
            started,
            "succeeded",
            ", ".join(f"{s.action} {s.kind}" for s in plan.steps)[:200],
        )
        await self.verify(target, deployment, plan)

    async def verify(self, target: Target, deployment: TargetDeployment, plan: TargetPlan) -> None:
        await self.check_not_stopped()
        started = await self.stage_start("verify", "deployer")
        health = await target.verify(deployment)
        await self.event(
            "deployer",
            VerifyResult(
                ok=health.ok,
                checks=health.checks,
                duration_ms=health.duration_ms,
                logs_tail=health.logs_tail,
            ),
        )
        if health.ok:
            live_url = health.live_url or deployment.live_url
            deployment = deployment.model_copy(update={"live_url": live_url})
            self.state["deployment"] = deployment.model_dump(mode="json")
            await self.stage_end(
                "verify", "deployer", started, "succeeded", "Rollout and health checks passed"
            )
            resource_id = await self.register(deployment)
            await self.event(
                "deployer",
                LiveUrl(
                    url=live_url or "", target=plan.target, app=plan.app, resource_id=resource_id
                ),
            )
            await self.record("live", deployment, plan)
            await self.finish(
                RunStatus.succeeded, StopReason.completed, live_url=live_url, deploy_status="live"
            )
        failing = "; ".join(f"{c.name}: {c.detail}" for c in health.checks if not c.ok)
        await self.stage_end("verify", "deployer", started, "failed", failing[:200])
        await self.offer_rollback(target, deployment, failing)

    async def offer_rollback(
        self, target: Target, deployment: TargetDeployment, reason: str
    ) -> None:
        """A failed verify: the logs are on the verify step; a rollback (or, on a first deploy,
        deleting the new deployment) is offered as an emergency approval. Never automatic."""
        plan = await target.rollback_plan(deployment)
        self.state["rollback_plan"] = plan.model_dump(mode="json")
        approval = Approval(
            run_id=self.run.id,
            workspace_id=self.run.workspace_id,
            tool_call_id="deploy-rollback",
            tool_name=TARGET_ROLLBACK,
            policy=ToolPolicy.emergency,
            arguments={
                "plan_hash": plan.plan_hash(),
                "app": plan.app,
                "target": plan.target,
                "to_revision": plan.rollback_to_revision,
            },
            status=ApprovalStatus.pending,
        )
        self.session.add(approval)
        await self.session.flush()
        self.state["rollback_approval_id"] = str(approval.id)
        await self.event(
            "deployer",
            RollbackOffered(
                approval_id=approval.id,
                reason=f"Verify failed: {reason}"[:300],
                from_revision=deployment.revision or "current",
                to_revision=plan.rollback_to_revision,
            ),
        )
        await record_step(
            self.session,
            self.run,
            kind="approval_requested",
            agent="deployer",
            tool_name=TARGET_ROLLBACK,
            approval_id=approval.id,
            output_summary="Waiting for approval to roll back",
            data=deploy_plan_card(plan, []),
        )
        await self.pause(AWAIT_ROLLBACK)

    async def after_rollback_decision(self) -> None:
        approval = await self.session.get(Approval, uuid.UUID(self.state["rollback_approval_id"]))
        await self.session.commit()
        if approval is None or approval.status == ApprovalStatus.pending:
            await self.pause(AWAIT_ROLLBACK)
            return
        deployment = TargetDeployment.model_validate(self.state["deployment"])
        plan = TargetPlan.model_validate(self.state["target_plan"])
        if approval.status == ApprovalStatus.rejected:
            await self.record("failed", deployment, plan)
            await self.finish(
                RunStatus.failed,
                StopReason.error,
                error="Verify failed and the rollback was rejected; the deployment stays in place.",
                deploy_status="failed",
            )
        rollback = TargetPlan.model_validate(self.state["rollback_plan"])
        target = await self.target()
        token = await issue_approved_action(
            self.session,
            approval,
            run_id=self.run.id,
            action=TARGET_ROLLBACK,
            plan_hash=rollback.plan_hash(),
        )
        await self.session.commit()
        started = await self.stage_start("rollback", "deployer")
        try:
            done = await target.rollback(rollback, token)
        except TargetError as exc:
            await self.stage_end("rollback", "deployer", started, "failed", str(exc)[:200])
            await self.finish(
                RunStatus.failed, StopReason.error, error=str(exc), deploy_status="failed"
            )
        what = (
            "Deleted the new deployment"
            if done.status == "deleted"
            else f"Back to {rollback.rollback_to_revision}"
        )
        await self.stage_end("rollback", "deployer", started, "succeeded", what)
        await self.record("rolled_back", deployment, plan)
        await self.finish(
            RunStatus.failed,
            StopReason.error,
            error="Verify failed; the deploy was rolled back.",
            deploy_status="rolled_back",
        )

    # --- records -------------------------------------------------------------------------

    async def register(self, deployment: TargetDeployment) -> uuid.UUID | None:
        """ResourceSink.register_deployment exactly once per run, and the resource is marked
        actionable (a target adapter can act on it)."""
        if self.state.get("resource_id"):
            return uuid.UUID(self.state["resource_id"])
        if not deployment.external_id:
            return None
        request = self.request
        k8s = deployment.target == "k8s"
        resource = DiscoveredResource(
            provider="k8s" if k8s else "aws",
            type="k8s:deployment" if k8s else "ecs:service",
            external_id=deployment.external_id,
            name=slug(deployment.app),
            region=deployment.location if k8s else self.settings.gc_aws_region,
            config={
                "image": deployment.image,
                "live_url": deployment.live_url,
                "deployed_by_run": str(self.run.id),
            },
        )
        sink_deployment = SinkDeployment(
            resource=resource,
            connection_id=uuid.UUID(request["target_connection_id"]),
            run_id=self.run.id,
            repo=request["source"]["repo"],
            commit=self.state.get("commit"),
            image=deployment.image,
            summary=f"Deployed by Groundcrew run {self.run.id}",
        )
        resource_id = await self.deps.sink.register_deployment(
            self.run.workspace_id, sink_deployment
        )
        if resource_id.int:
            await self.session.execute(
                update(Resource).where(Resource.id == resource_id).values(coverage="actionable")
            )
        self.state["resource_id"] = str(resource_id)
        await self.save()
        return resource_id

    async def record(
        self, status: DeployStatus, deployment: TargetDeployment, plan: TargetPlan
    ) -> None:
        request = self.request
        source = request["source"]
        self.state["deploy"] = DeployRecord(
            target=deployment.target,
            connection_id=deployment.connection_id,
            app=deployment.app,
            image=deployment.image,
            commit=self.state.get("commit"),
            repo=source["repo"] if source["kind"] == "github" else f"fixture:{source['repo']}",
            plan_summary=plan_summary(plan),
            live_url=deployment.live_url,
            revision=deployment.revision,
            previous_revision=deployment.previous_revision,
            started_at=self.run.started_at or _now(),
            finished_at=_now(),
            status=status,
        ).model_dump(mode="json")
        await self.save()


def _where(plan: TargetPlan) -> str:
    return "Kubernetes" if plan.target == "k8s" else "AWS ECS"


def _secret(value: str) -> Any:
    from pydantic import SecretStr

    return SecretStr(value)


def _seconds_since(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return max(0.0, (_now() - datetime.fromisoformat(iso)).total_seconds())
    except ValueError:
        return 0.0


async def pipeline_job(session_factory: SessionFactory, payload: dict[str, Any]) -> None:
    """Worker handler for "deploy" jobs (and "resume_run" jobs of deploy runs)."""
    await PipelineRunner(session_factory).run(uuid.UUID(payload["run_id"]))


async def resume_run_job(session_factory: SessionFactory, payload: dict[str, Any]) -> None:
    """Worker handler for "resume_run": deploy runs go to the pipeline, other runs to the
    agent loop (decide_approval enqueues the same job kind for both)."""
    from app.agents.base import run_agent_job

    async with session_factory() as session:
        agent = await session.scalar(
            select(Run.agent).where(Run.id == uuid.UUID(payload["run_id"]))
        )
    if agent == "deploy":
        await pipeline_job(session_factory, payload)
    else:
        await run_agent_job(session_factory, payload)


async def count_steps(session: AsyncSession, run_id: uuid.UUID, kind: str) -> int:
    from app.db.models import RunStep

    return int(
        await session.scalar(
            select(func.count(RunStep.id)).where(RunStep.run_id == run_id, RunStep.kind == kind)
        )
        or 0
    )


__all__ = [
    "DEPLOY_JOB",
    "PipelineDeps",
    "PipelineRunner",
    "PipelineState",
    "count_steps",
    "default_deps",
    "open_env",
    "pipeline_job",
    "purge_env",
    "resume_run_job",
    "seal_env",
]
