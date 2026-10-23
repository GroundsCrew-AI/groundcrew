"""Deploys: Scout -> Builder -> Deployer in one run (docs/deploys-contract.md).

A deploy is a run with agent="deploy"; its id is the run's id. The request (without env
values) lives in runs.state["request"]; env values go Fernet-encrypted into the "deploy" job
only (app.pipeline.runner), and are purged when the run ends.
"""

import base64
import binascii
import uuid
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import and_, or_, select

from app import queue, quotas
from app.accounts import NotFoundError, member_workspace
from app.agents.scout.detectors import is_manifest, service_dirs
from app.audit import record
from app.connectors.github import GitHubError, get_github_app
from app.db.models import (
    Approval,
    ApprovalStatus,
    Connection,
    Run,
    RunStatus,
    RunStep,
    WorkspaceRole,
)
from app.pipeline.events import DeployPlan, EnvVarView, StageFinished
from app.pipeline.runner import DEPLOY_JOB, deploy_job_row, open_env, seal_env
from app.pipeline.state import DeployRecord
from app.policy.engine import ApprovalStateError, Decision, decide_approval
from app.routes.deploy_schemas import (
    DeployCreate,
    DeployCreated,
    DeployDetail,
    DeployEnvUpdate,
    DeployEnvUpdated,
    DeployPage,
    DeploySourceOut,
    DeployState,
    DeploySummary,
    DeployTargetOut,
    FixtureSource,
    RepoService,
    RepoServices,
    StageOut,
)
from app.routes.deps import ClientIp, CurrentUser, SessionDep, SettingsDep
from app.steps import record_step

router = APIRouter(prefix="/deploys", tags=["deploys"])


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


async def _connection(
    session: SessionDep, workspace_id: uuid.UUID, conn_id: uuid.UUID
) -> Connection:
    conn = await session.get(Connection, conn_id)
    if conn is None or conn.workspace_id != workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found.")
    return conn


async def _deploy_run(
    session: SessionDep,
    user: CurrentUser,
    deploy_id: uuid.UUID,
    minimum: WorkspaceRole = WorkspaceRole.viewer,
) -> Run:
    run = await session.get(Run, deploy_id)
    if run is None or run.agent != "deploy":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Deploy not found.")
    try:
        await member_workspace(session, user, run.workspace_id, minimum)
    except NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Deploy not found.") from None
    return run


@router.get(
    "/services",
    responses={409: {"description": "The connection is not GitHub, or GitHub refused."}},
)
async def repo_services(
    user: CurrentUser,
    session: SessionDep,
    workspace_id: Annotated[uuid.UUID, Query()],
    connection_id: Annotated[uuid.UUID, Query()],
    repo: Annotated[str, Query(max_length=140)],
    ref: Annotated[str, Query(min_length=1, max_length=255)],
) -> RepoServices:
    """The folders of a repository that deploy on their own (api/, web/, services/billing/),
    read from GitHub without cloning. The deploy form asks which one when there are several."""
    await _check_member(session, user, workspace_id)
    conn = await _connection(session, workspace_id, connection_id)
    if conn.kind != "github":
        raise HTTPException(status.HTTP_409_CONFLICT, "The source connection is not GitHub.")
    try:
        paths, _ = await get_github_app().tree_paths(int(conn.config["installation_id"]), repo, ref)
    except (GitHubError, ValueError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    dirs = service_dirs(paths)
    manifests: dict[str, list[str]] = {d: [] for d in dirs}
    for rel in paths:
        path = PurePosixPath(rel)
        if is_manifest(path.name) and path.parent.as_posix() in manifests:
            manifests[path.parent.as_posix()].append(path.name)
    return RepoServices(
        services=[RepoService(path=d, manifests=sorted(manifests[d])) for d in dirs],
        choose="." not in dirs and len(dirs) > 1,
    )


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    responses={409: {"description": "Target is AWS observe tier, or the source is not GitHub."}},
)
async def create_deploy(
    body: DeployCreate,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    ip: ClientIp,
) -> DeployCreated:
    """Start a deploy run. Returns at once; follow the run's event stream for progress.

    Nothing touches infrastructure before a workspace member approves the rendered plan.
    """
    await _check_member(session, user, body.workspace_id, WorkspaceRole.approver)
    await quotas.check_can_start_run(session, body.workspace_id, settings)
    if isinstance(body.repo, FixtureSource):
        if settings.app_env != "dev":
            raise HTTPException(status.HTTP_409_CONFLICT, "Fixtures are for local dev only.")
        source: dict[str, Any] = {
            "kind": "fixture",
            "repo": body.repo.fixture,
            "ref": None,
            "path": body.repo.path,
            "connection_id": None,
        }
        label = f"fixture {body.repo.fixture}"
    else:
        repo_conn = await _connection(session, body.workspace_id, body.repo.connection_id)
        if repo_conn.kind != "github":
            raise HTTPException(status.HTTP_409_CONFLICT, "The source connection is not GitHub.")
        source = {
            "kind": "github",
            "repo": body.repo.repo,
            "ref": body.repo.ref,
            "path": body.repo.path,
            "connection_id": str(repo_conn.id),
        }
        label = f"{body.repo.repo}@{body.repo.ref}"
    if body.repo.path not in (None, "."):
        label += f" ({body.repo.path})"
    target = await _connection(session, body.workspace_id, body.target_connection_id)
    if target.kind == "aws":
        if (target.config.get("tier") or "observe") != "deploy":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{target.name} is connected for observing only. Turn on deploys for it on the "
                "Connections page (Tier=deploy).",
            )
        kind = "aws_ecs"
    elif target.kind == "k8s":
        kind = "k8s"
    elif target.kind == "agent":
        from app import registry
        from app.targets.agent import deployable_services, own_registry

        kind = "agent"
        allowed = deployable_services(target)
        if body.target_service not in allowed:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Choose a service on {target.name} that allows deploy"
                + (
                    f": {', '.join(allowed)}."
                    if allowed
                    else ": none does yet. Add deploy to a "
                    "service's `allow` list in /etc/groundcrew-agent/agent.yaml."
                ),
            )
        if own_registry(target) is None and not registry.enabled(settings):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Add a registry for {target.name} on the Connections page first.",
            )
    else:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{target.name} is not a deploy target.")

    run_id = uuid.uuid4()
    request = {
        "source": source,
        "target_connection_id": str(target.id),
        "target": {"kind": kind, "name": target.name, "service": body.target_service},
        "env": [
            {"name": v.name, "secret": v.secret, "provided": bool(v.value.get_secret_value())}
            for v in body.env
        ],
        "allow_missing_env": body.options.allow_missing_env,
        "created_by": str(user.id),
    }
    run = Run(
        id=run_id,
        workspace_id=body.workspace_id,
        agent="deploy",
        goal=f"Deploy {label} to {target.name}",
        status=RunStatus.queued,
        state={"deploy_id": str(run_id), "request": request, "phase": "start", "stages": {}},
    )
    session.add(run)
    await session.flush()
    env = [
        {"name": v.name, "value": v.value.get_secret_value(), "secret": v.secret} for v in body.env
    ]
    await queue.enqueue(
        session, DEPLOY_JOB, {"run_id": str(run_id), "env": seal_env(env)}, max_attempts=1
    )
    record(
        session,
        workspace_id=body.workspace_id,
        actor=user,
        action="deploy.started",
        target_type="run",
        target_id=run_id,
        detail={"source": label},
        ip=ip,
    )
    await session.commit()
    return DeployCreated(deploy_id=run_id, run_id=run_id)


# --- reading ------------------------------------------------------------------------------


def _deploy_state(run: Run, state: dict[str, Any]) -> DeployState:
    deploy_status = (state.get("final") or {}).get("deploy_status")
    match run.status:
        case RunStatus.queued:
            return "queued"
        case RunStatus.running:
            return "running"
        case RunStatus.waiting_approval:
            return "waiting_approval"
        case RunStatus.succeeded:
            return "live" if deploy_status == "live" else "stopped"
        case RunStatus.failed:
            return "rolled_back" if deploy_status == "rolled_back" else "failed"
    return "stopped"


def summary(run: Run) -> DeploySummary:
    state: dict[str, Any] = dict(run.state or {})
    request = state.get("request") or {}
    source = request.get("source") or {}
    target = request.get("target") or {}
    build = state.get("build") or {}
    record = state.get("deploy") or {}
    started = run.started_at
    duration = (
        int(((run.finished_at or datetime.now(started.tzinfo)) - started).total_seconds() * 1000)
        if started
        else None
    )
    kind: Literal["github", "fixture"] = "fixture" if source.get("kind") == "fixture" else "github"
    conn = source.get("connection_id")
    created_by = request.get("created_by")
    return DeploySummary(
        id=run.id,
        run_id=run.id,
        workspace_id=run.workspace_id,
        status=_deploy_state(run, state),
        run_status=run.status,
        stop_reason=run.stop_reason,
        stage=state.get("stage"),
        app=state.get("app"),
        source=DeploySourceOut(
            kind=kind,
            repo=str(source.get("repo") or ""),
            ref=source.get("ref"),
            path=source.get("path"),
            connection_id=uuid.UUID(conn) if conn else None,
            commit=state.get("commit"),
        ),
        target=DeployTargetOut(
            connection_id=uuid.UUID(request.get("target_connection_id") or str(uuid.UUID(int=0))),
            kind=target.get("kind") or "k8s",
            name=str(target.get("name") or ""),
        ),
        live_url=(state.get("final") or {}).get("live_url") or record.get("live_url"),
        build_attempts=int(build.get("attempts") or 0),
        automatic_fix=bool(build.get("automatic_fix")),
        cost_usd=float(run.cost_usd) if run.cost_usd is not None else None,
        ultra_equiv_usd=float(run.ultra_equiv_usd) if run.ultra_equiv_usd is not None else None,
        error=run.error,
        created_by=uuid.UUID(created_by) if created_by else None,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=duration,
    )


def _encode_cursor(run: Run) -> str:
    raw = f"{run.created_at.isoformat()}|{run.id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        created, _, run_id = raw.partition("|")
        return datetime.fromisoformat(created), uuid.UUID(run_id)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid cursor.") from None


@router.get("")
async def list_deploys(
    user: CurrentUser,
    session: SessionDep,
    workspace_id: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> DeployPage:
    """Newest first. Pass next_cursor back as ?cursor= for the next page."""
    await _check_member(session, user, workspace_id)
    query = (
        select(Run)
        .where(Run.workspace_id == workspace_id, Run.agent == "deploy")
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(limit + 1)
    )
    if cursor:
        created, run_id = _decode_cursor(cursor)
        query = query.where(
            or_(Run.created_at < created, and_(Run.created_at == created, Run.id < run_id))
        )
    runs = list(await session.scalars(query))
    has_more = len(runs) > limit
    runs = runs[:limit]
    return DeployPage(
        items=[summary(r) for r in runs], next_cursor=_encode_cursor(runs[-1]) if has_more else None
    )


async def _latest_plan(session: SessionDep, run_id: uuid.UUID) -> DeployPlan | None:
    steps = await session.scalars(
        select(RunStep)
        .where(RunStep.run_id == run_id, RunStep.kind == "approval_requested")
        .order_by(RunStep.seq.desc())
    )
    for step in steps:
        data = step.data if isinstance(step.data, dict) else None
        if data and data.get("type") == "deploy_plan" and not data.get("rollback_to_revision"):
            return DeployPlan.model_validate(data)
    return None


@router.get("/{deploy_id}")
async def get_deploy(deploy_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> DeployDetail:
    """A deploy with its stages, plan (secrets masked), and final deploy record."""
    run = await _deploy_run(session, user, deploy_id)
    state: dict[str, Any] = dict(run.state or {})
    request = state.get("request") or {}
    required = {
        v["name"] for v in (state.get("spec") or {}).get("env_vars", []) if v.get("required")
    }
    env = [
        EnvVarView(
            name=v["name"],
            secret=bool(v.get("secret")),
            required=v["name"] in required,
            provided=bool(v.get("provided", True)),
            value=None,
        )
        for v in request.get("env", [])
    ]
    stages = [StageOut(stage=name, **value) for name, value in (state.get("stages") or {}).items()]
    record = state.get("deploy")
    return DeployDetail(
        **summary(run).model_dump(),
        env=env,
        allow_missing_env=bool(request.get("allow_missing_env")),
        stages=stages,
        plan=await _latest_plan(session, run.id),
        deploy=DeployRecord.model_validate(record) if record else None,
    )


# --- fixing env at the approval card ------------------------------------------------------


@router.post(
    "/{deploy_id}/env",
    status_code=status.HTTP_202_ACCEPTED,
    responses={409: {"description": "The approval is not this deploy's pending plan."}},
)
async def update_env(
    deploy_id: uuid.UUID, body: DeployEnvUpdate, user: CurrentUser, session: SessionDep
) -> DeployEnvUpdated:
    """Set missing env values, or continue without them. The pending plan approval is
    replaced: only preflight runs again and the plan is re-rendered with the same built image
    (no rebuild). Values are handled like POST /deploys: encrypted in the job, never echoed."""
    run = await _deploy_run(session, user, deploy_id, WorkspaceRole.approver)
    run = await session.get_one(Run, run.id, with_for_update=True, populate_existing=True)
    state: dict[str, Any] = dict(run.state or {})
    approval = await session.get(Approval, body.approval_id)
    if (
        approval is None
        or approval.run_id != run.id
        or str(approval.id) != state.get("plan_approval_id")
        or approval.status != ApprovalStatus.pending
        or state.get("phase") != "awaiting_plan_approval"
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "That is not this deploy's pending plan.")

    job = await deploy_job_row(session, run.id)
    if job is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This deploy can no longer take env vars.")
    current = open_env(job.payload.get("env") if isinstance(job.payload, dict) else None)
    for var in body.env:
        current[var.name] = (var.value.get_secret_value(), var.secret)
    job.payload = {
        **job.payload,
        "env": seal_env([{"name": n, "value": v, "secret": s} for n, (v, s) in current.items()]),
    }
    request = dict(state.get("request") or {})
    names = {v["name"]: v for v in request.get("env", [])}
    for var in body.env:
        names[var.name] = {
            "name": var.name,
            "secret": var.secret,
            "provided": bool(var.value.get_secret_value()),
        }
    request["env"] = list(names.values())
    request["allow_missing_env"] = bool(request.get("allow_missing_env")) or body.allow_missing_env
    state["request"] = request
    state["replan"] = True
    run.state = state
    set_names = [v.name for v in body.env if v.value.get_secret_value()]
    reason = (
        f"{', '.join(set_names)} set" if set_names else "continuing without the missing env vars"
    )
    await record_step(
        session,
        run,
        kind="stage_finished",
        agent="deployer",
        data=StageFinished(
            stage="approval",
            agent="deployer",
            status="skipped",
            duration_ms=0,
            summary=f"Plan replaced: {reason}",
        ),
    )
    await session.commit()
    try:
        await decide_approval(session, approval.id, Decision.reject, user.id)
    except ApprovalStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return DeployEnvUpdated(approval_id=approval.id)
