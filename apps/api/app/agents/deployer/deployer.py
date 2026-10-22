"""Deployer (Phase 4, M5): plan the deploy, show the exact plan for approval, apply, verify.

    deployer = Deployer(llm)
    draft = await deployer.draft(spec, env, target_kind, budget=..., recorder=...)  # plan_deploy
    spec = deploy_spec(...)                  # what the target needs (secret values included)
    plan = await target.plan(spec)           # rendered, secrets masked; plan_hash() binds approval
    card = deploy_plan_card(plan, checks)    # the approval card's DeployPlan

The model decides replicas, size, and which env vars are secret. It can only make a variable
more secret, never less: a value the user marked secret, or Scout found secret, stays secret.
Applying needs an ApprovedAction; the pipeline (M6) asks for the approval, issues the token, and
keeps secret values out of state, approvals, steps, and logs.
"""

import json
import uuid
from typing import Literal

from app.agents.deployer.schemas import DeployDraft
from app.agents.scout.appspec import AppSpec
from app.llm.budget import RunBudget
from app.llm.client import ChatModel, LLMError, OnCall
from app.llm.router import Task
from app.pipeline.events import DeployPlan, PlannedResource, PreflightCheck
from app.targets.base import DeploySpec, EnvValue, TargetPlan

SYSTEM = """You are Deployer, part of Groundcrew. You plan how to run an app that is already \
built into a container image. Choose:
- replicas: 1 unless the app clearly needs more (this is a demo-sized deploy; at most 3),
- size: "small" unless the app clearly needs more memory or CPU,
- secret_env: every env var whose value is a secret (passwords, API keys, tokens, connection \
strings with credentials). When unsure, treat it as secret.
Give a one-paragraph rationale."""


def default_draft(spec: AppSpec, env_names: list[str]) -> DeployDraft:
    """What Deployer uses when the model call fails: one small replica, Scout's secrets."""
    secrets = sorted({v.name for v in spec.env_vars if v.secret})
    return DeployDraft(
        replicas=1, size="small", secret_env=secrets, rationale="Defaults (no model answer)."
    )


def _messages(spec: AppSpec, env: list[tuple[str, bool]], target: str) -> list[dict[str, str]]:
    facts = {
        "language": spec.language,
        "framework": spec.framework,
        "start": spec.start_cmd,
        "port": spec.port,
        "health_path": spec.health_path,
        "database": spec.database,
        "env": [
            {"name": v.name, "required": v.required, "secret_per_scout": v.secret}
            for v in spec.env_vars
        ],
        "env_given_by_user": [{"name": n, "marked_secret": s} for n, s in env],
        "target": target,
    }
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": json.dumps(facts, default=str, indent=1)},
    ]


class Deployer:
    def __init__(self, llm: ChatModel) -> None:
        self.llm = llm

    async def draft(
        self,
        spec: AppSpec,
        env: list[tuple[str, bool]],
        target: str,
        *,
        budget: RunBudget,
        recorder: OnCall,
    ) -> tuple[DeployDraft, str | None]:
        """plan_deploy. Returns the draft and a note when the defaults were used instead."""
        try:
            result = await self.llm.chat(
                Task.plan_deploy,
                _messages(spec, env, target),
                budget=budget,
                schema=DeployDraft,
                on_call=recorder,
            )
        except LLMError as exc:
            return default_draft(spec, [n for n, _ in env]), f"plan_deploy failed, defaults: {exc}"
        draft: DeployDraft = result.parsed
        return draft, None


def deploy_spec(
    *,
    run_id: uuid.UUID,
    connection_id: uuid.UUID,
    app: str,
    spec: AppSpec,
    draft: DeployDraft,
    env_values: dict[str, tuple[str, bool]],
    source_image: str,
    source_image_id: str,
    smoke_passed: bool,
    tag: str,
    port: int,
    allow_missing_env: bool,
    repo: str | None,
    commit: str | None,
    target_service: str | None = None,
) -> DeploySpec:
    """The target's input. A variable is secret if the user, Scout, or the model said so."""
    scout_secret = {v.name for v in spec.env_vars if v.secret}
    model_secret = set(draft.secret_env)
    env = [
        EnvValue(
            name=name,
            value=value,
            secret=marked or name in scout_secret or name in model_secret,
        )
        for name, (value, marked) in sorted(env_values.items())
    ]
    required = sorted(v.name for v in spec.env_vars if v.required)
    return DeploySpec(
        run_id=run_id,
        connection_id=connection_id,
        app=app,
        source_image=source_image,
        source_image_id=source_image_id,
        smoke_passed=smoke_passed,
        tag=tag,
        port=port,
        health_path=spec.health_path,
        replicas=draft.replicas,
        env=env,
        required_env=required,
        allow_missing_env=allow_missing_env,
        repo=repo,
        commit=commit,
        target_service=target_service,
    )


def deploy_plan_card(plan: TargetPlan, checks: list[PreflightCheck]) -> DeployPlan:
    """The approval card for a TargetPlan: the same plan, in the M1 event shape."""
    return DeployPlan(
        target=plan.target,
        target_connection_id=plan.connection_id,
        app=plan.app,
        image=plan.image,
        source_image=plan.source_image_id,
        port=plan.port,
        health_path=plan.health_path,
        replicas=plan.replicas,
        cpu=plan.cpu,
        memory=plan.memory,
        env=plan.env,
        resources=[
            PlannedResource(kind=s.kind, name=s.name, action=s.action, detail=s.detail)
            for s in plan.steps
        ],
        preflight=checks,
        allow_missing_env=plan.allow_missing_env,
        expected_url=plan.expected_url,
        rendered=plan.rendered,
        rendered_format=plan.rendered_format,
        rollback_to_revision=plan.rollback_to_revision,
    )


def blocked(checks: list[PreflightCheck]) -> list[PreflightCheck]:
    """Failed checks that were not overridden: the plan cannot be approved."""
    return [c for c in checks if c.status == "fail" and not c.overridden]


def plan_summary(plan: TargetPlan) -> str:
    if plan.target == "agent":
        return f"Container {json.loads(plan.rendered).get('service', plan.app)} on the server"
    kind: Literal["Deployment", "ECS service"] = (
        "Deployment" if plan.target == "k8s" else "ECS service"
    )
    return f"{kind} {plan.app}, {plan.replicas} replica, {plan.cpu} / {plan.memory}"
