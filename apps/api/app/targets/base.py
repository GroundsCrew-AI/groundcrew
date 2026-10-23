"""The deploy target contract (PROJECT_CONTEXT.md section 8, docs/phase4-infra.md, Targets).

A target takes an image that Builder built and smoke-tested in the sandbox to a running app:

    discover() -> what this target already runs (graph resources)
    plan(spec) -> TargetPlan: the ordered apply steps and the rendered config, secrets masked
    preflight(plan) -> checks, read-only; a failed check (not overridden) blocks the approval
    apply(plan, spec, token, sandbox) -> Deployment       needs ApprovedAction(target_apply)
    verify(deployment) -> Health: rollout, health path, root
    logs(deployment, since) -> LogLine list
    rollback_plan(deployment) -> TargetPlan for a rollback (or deleting a first deploy)
    rollback(plan, token) -> Deployment                    needs ApprovedAction(target_rollback)

Nothing leaves the sandbox before approval: the plan's first apply step is the image push
(after creating the ECR repository on ECS), and preflight never pushes or writes.

Secret values are never in a plan: the plan is what gets approved, stored, and shown. They
come to apply() in the DeploySpec, which must match the plan.
"""

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, Field, SecretStr

from app.discovery.types import ConnectorResult
from app.pipeline.events import EnvVarView, PreflightCheck, TargetKind, VerifyCheck
from app.policy.actions import ApprovedAction
from app.sandbox.base import Sandbox


class TargetError(RuntimeError):
    """A target call failed. The message says what happened, never a secret value."""


class PlanMismatchError(TargetError):
    """apply() was given a spec that does not match the approved plan."""


class EnvValue(BaseModel):
    name: str
    value: SecretStr
    secret: bool = False


class DeploySpec(BaseModel):
    """What the pipeline knows when it asks a target for a plan (Scout + Builder + the user)."""

    run_id: uuid.UUID
    connection_id: uuid.UUID
    app: str
    # The image in the sandbox that passed the smoke test (what the sandbox pushes), and its
    # sha256 ID (what the approval pins and preflight shows).
    source_image: str
    source_image_id: str
    smoke_passed: bool
    # Image tag in the registry: the commit's first 12 characters, or the run id for fixtures.
    tag: str
    port: int = Field(ge=1, le=65535)
    health_path: str | None = None
    replicas: int = Field(default=1, ge=1, le=3)
    env: list[EnvValue] = Field(default_factory=list)
    # Names Scout marked required. Missing ones fail preflight unless allow_missing_env.
    required_env: list[str] = Field(default_factory=list)
    allow_missing_env: bool = False
    repo: str | None = None
    commit: str | None = None
    # Agent targets: the service on the server this deploy updates (its `allow` lists deploy).
    target_service: str | None = None
    # A settings change (POST /deploys/{id}/settings): the image already in the registry
    # (`source_image` is its reference) runs again with other env values; nothing is built or
    # pushed. `keep_env`: names whose value stays as it is on the target (secrets: Groundcrew
    # never stores values); `remove_env`: names taken off the app. `previous_env`: the names
    # the app had, so the plan can say what is added and what is updated.
    config_change: bool = False
    keep_env: list[str] = Field(default_factory=list)
    # Which kept names are secrets (the rest are plain values).
    keep_secret: list[str] = Field(default_factory=list)
    remove_env: list[str] = Field(default_factory=list)
    previous_env: list[str] = Field(default_factory=list)


class ApplyStep(BaseModel):
    """One step of apply, in order. The image push is first on every target."""

    kind: str = Field(examples=["image", "ecr:Repository", "Deployment", "ecs:Service"])
    name: str
    action: Literal["create", "update", "unchanged", "delete", "push"]
    detail: str | None = None


class TargetPlan(BaseModel):
    """The exact plan a person approves. `plan_hash()` binds the approval to it."""

    action: Literal["apply", "rollback"] = "apply"
    target: TargetKind
    connection_id: uuid.UUID
    run_id: uuid.UUID
    app: str
    # Where the image is pushed and what the workload runs.
    image: str
    source_image_id: str | None
    image_tested: bool
    port: int
    health_path: str | None
    replicas: int
    cpu: str
    memory: str
    env: list[EnvVarView]
    allow_missing_env: bool
    steps: list[ApplyStep]
    # Manifests (k8s, YAML) or task definition and service (ECS, JSON); secrets "********".
    rendered: str
    rendered_format: Literal["yaml", "json"]
    expected_url: str | None = None
    # The revision running before this plan (what a rollback returns to); null when new.
    previous_revision: str | None = None
    # Rollback plans: the revision to go back to, or null to delete a first deploy.
    rollback_to_revision: str | None = None

    def plan_hash(self) -> str:
        """sha256 of the canonical JSON of the plan (no secret values are in it)."""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class Deployment(BaseModel):
    """A deployment a target made. Stored by the pipeline in runs.state (DeployRecord)."""

    target: TargetKind
    connection_id: uuid.UUID
    run_id: uuid.UUID
    app: str
    image: str
    # Deployment revision (k8s) or task definition ARN (ECS).
    revision: str | None
    previous_revision: str | None
    # k8s: namespace. ECS: cluster/service.
    location: str
    live_url: str | None = None
    port: int
    health_path: str | None
    # Graph resource id (ResourceSink external_id) for "View on Map".
    external_id: str | None = None
    status: Literal["applied", "rolled_back", "deleted"] = "applied"
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Health(BaseModel):
    ok: bool
    checks: list[VerifyCheck]
    duration_ms: int
    live_url: str | None = None
    # On failure: the app's last log lines, redacted.
    logs_tail: str | None = None


class LogLine(BaseModel):
    at: datetime | None
    # Pod name (k8s) or log stream (ECS).
    source: str
    message: str


class Target(Protocol):
    kind: TargetKind

    async def discover(self) -> ConnectorResult: ...

    async def plan(self, spec: DeploySpec) -> TargetPlan: ...

    async def preflight(self, plan: TargetPlan) -> list[PreflightCheck]: ...

    async def apply(
        self, plan: TargetPlan, spec: DeploySpec, token: ApprovedAction, sandbox: Sandbox
    ) -> Deployment: ...

    async def verify(self, deployment: Deployment) -> Health: ...

    async def logs(self, deployment: Deployment, since: datetime) -> list[LogLine]: ...

    async def rollback_plan(self, deployment: Deployment) -> TargetPlan: ...

    async def rollback(self, plan: TargetPlan, token: ApprovedAction) -> Deployment: ...


MASK = "********"


def env_views(spec: DeploySpec) -> list[EnvVarView]:
    """The plan's env: every given var plus every required one, secrets without values. On a
    settings change, also the kept and removed names, each with what happens to it."""
    given = {v.name: v for v in spec.env}
    kept, removed = set(spec.keep_env), set(spec.remove_env)
    names = sorted(set(given) | (set(spec.required_env) - removed) | kept | removed)
    before = set(spec.previous_env)
    views = []
    for name in names:
        var = given.get(name)
        provided = (var is not None and bool(var.value.get_secret_value())) or name in kept
        if var is not None:
            secret = var.secret
        elif name in kept:
            secret = name in spec.keep_secret
        else:
            secret = True
        change: Literal["add", "update", "keep", "remove"] | None = None
        if spec.config_change:
            if name in removed:
                change, provided = "remove", False
            elif name in kept:
                change = "keep"
            else:
                change = "update" if name in before else "add"
        views.append(
            EnvVarView(
                name=name,
                secret=secret,
                required=name in spec.required_env and name not in removed,
                provided=provided,
                value=var.value.get_secret_value()
                if var and var.value.get_secret_value() and not secret
                else None,
                change=change,
            )
        )
    return views


def common_checks(plan: TargetPlan) -> list[PreflightCheck]:
    """Preflight checks every target makes from the plan alone (image, env, port)."""
    short = (plan.source_image_id or "")[:19]
    settings_change = any(v.change is not None for v in plan.env) or not any(
        s.action == "push" for s in plan.steps
    )
    image = PreflightCheck(
        name="image",
        status="pass" if plan.image_tested else "fail",
        detail=(
            f"The image running now ({short}); nothing is built or pushed"
            if settings_change and plan.image_tested
            else f"Built and smoke-tested ({short}); pushed after approval"
            if plan.image_tested
            else f"The image ({short}) did not pass the smoke test in the sandbox"
        ),
    )
    missing = [v.name for v in plan.env if v.required and not v.provided]
    env = PreflightCheck(
        name="env",
        status="fail" if missing else "pass",
        detail=f"Missing required: {', '.join(missing)}"
        if missing
        else "All required env vars set",
        overridden=bool(missing) and plan.allow_missing_env,
    )
    port = PreflightCheck(
        name="port",
        status="pass" if 1 <= plan.port <= 65535 else "fail",
        detail=f"The app listens on {plan.port}",
    )
    return [image, env, port]


def check_spec_matches(plan: TargetPlan, spec: DeploySpec) -> None:
    """apply() runs the approved plan with the values in `spec`; refuse a spec that would
    deploy something else (another image, port, or env than the plan shows)."""
    problems = []
    if spec.run_id != plan.run_id or spec.connection_id != plan.connection_id:
        problems.append("run or connection")
    if spec.source_image_id != plan.source_image_id:
        problems.append("image")
    if spec.port != plan.port or spec.health_path != plan.health_path:
        problems.append("port or health path")
    if spec.replicas != plan.replicas:
        problems.append("replicas")
    if [v.model_dump() for v in env_views(spec)] != [v.model_dump() for v in plan.env]:
        problems.append("env vars")
    if problems:
        raise PlanMismatchError(
            f"The deploy spec does not match the approved plan ({', '.join(problems)})."
        )


def secret_values(spec: DeploySpec) -> dict[str, str]:
    return {
        v.name: v.value.get_secret_value()
        for v in spec.env
        if v.secret and v.value.get_secret_value()
    }


def plain_values(spec: DeploySpec) -> dict[str, str]:
    return {
        v.name: v.value.get_secret_value()
        for v in spec.env
        if not v.secret and v.value.get_secret_value()
    }
