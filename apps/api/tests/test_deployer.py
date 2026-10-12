"""Deployer on FakeLLM: plan_deploy, secret flags that only ever get stricter, and the approval
card built from a target plan."""

import json
import uuid
from typing import Any

import pytest

from app.agents.deployer import (
    Deployer,
    blocked,
    deploy_plan_card,
    deploy_spec,
    plan_summary,
)
from app.agents.deployer.schemas import DeployDraft
from app.agents.scout.appspec import AppSpec, EnvVar
from app.config import Settings
from app.llm.budget import MemoryUsageStore, RunBudget
from app.llm.client import LLMError, LLMResult
from app.pipeline.events import EnvVarView, PreflightCheck
from app.targets.base import ApplyStep, TargetPlan
from tests.fakes import FakeLLM, reply

pytestmark = pytest.mark.anyio

SPEC = AppSpec(
    language="python",
    framework="flask",
    port=8000,
    health_path="/health",
    env_vars=[
        EnvVar(name="DATABASE_URL", required=True, secret=True),
        EnvVar(name="MODE", required=False, secret=False),
        EnvVar(name="SESSION_KEY", required=True, secret=False),
    ],
)


def _settings() -> Settings:
    return Settings(_env_file=None, model_nano="m/nano", model_super="m/super")


async def _record(_result: LLMResult) -> None:
    return None


async def test_plan_deploy_asks_super_with_low_reasoning() -> None:
    answer = {"replicas": 1, "size": "small", "secret_env": ["SESSION_KEY"], "rationale": "demo"}
    llm = FakeLLM(reply(json.dumps(answer)), settings=_settings())
    draft, note = await Deployer(llm).draft(
        SPEC,
        [("DATABASE_URL", True), ("MODE", False)],
        "k8s",
        budget=RunBudget(usage=MemoryUsageStore(), settings=_settings()),
        recorder=_record,
    )
    assert note is None and draft.secret_env == ["SESSION_KEY"] and draft.replicas == 1
    assert llm.calls[0]["model"] == "m/super"
    sent = json.loads(llm.calls[0]["messages"][-1]["content"].split("\n\n")[0])
    assert sent["env_given_by_user"] == [
        {"name": "DATABASE_URL", "marked_secret": True},
        {"name": "MODE", "marked_secret": False},
    ]
    assert "hunter2" not in json.dumps(llm.calls)


async def test_a_failed_plan_deploy_uses_defaults() -> None:
    llm = FakeLLM(LLMError("down"), settings=_settings())
    draft, note = await Deployer(llm).draft(
        SPEC,
        [],
        "aws_ecs",
        budget=RunBudget(usage=MemoryUsageStore(), settings=_settings()),
        recorder=_record,
    )
    assert note and "defaults" in note
    assert draft.replicas == 1 and draft.secret_env == ["DATABASE_URL"]


def _spec(draft: DeployDraft, env: dict[str, tuple[str, bool]]) -> Any:
    return deploy_spec(
        run_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        app="legacy-flask-app",
        spec=SPEC,
        draft=draft,
        env_values=env,
        source_image="gc-build/x:1",
        source_image_id="sha256:" + "a" * 64,
        smoke_passed=True,
        tag="abc",
        port=8000,
        allow_missing_env=False,
        repo="GroundsCrew-AI/legacy-flask-app",
        commit="abc",
    )


def test_secret_flags_only_get_stricter() -> None:
    draft = DeployDraft(replicas=2, size="small", secret_env=["SESSION_KEY"], rationale="x")
    env = {
        "DATABASE_URL": ("postgresql://a:hunter2@db/x", False),  # Scout says secret
        "SESSION_KEY": ("k", False),  # the model says secret
        "MODE": ("production", False),
        "API_TOKEN": ("t", True),  # the user said secret
    }
    spec = _spec(draft, env)
    flags = {v.name: v.secret for v in spec.env}
    assert flags == {"API_TOKEN": True, "DATABASE_URL": True, "MODE": False, "SESSION_KEY": True}
    assert spec.replicas == 2 and spec.required_env == ["DATABASE_URL", "SESSION_KEY"]
    # The model cannot make a secret plain.
    plain = DeployDraft(replicas=1, size="small", secret_env=[], rationale="x")
    assert {v.name: v.secret for v in _spec(plain, env).env}["DATABASE_URL"] is True


def test_the_approval_card_is_the_target_plan() -> None:
    plan = TargetPlan(
        target="k8s",
        connection_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        app="legacy-flask-app",
        image="kind-registry:5000/legacy-flask-app:abc",
        source_image_id="sha256:" + "a" * 64,
        image_tested=True,
        port=8000,
        health_path="/health",
        replicas=1,
        cpu="250m",
        memory="512Mi",
        env=[EnvVarView(name="DATABASE_URL", secret=True, required=True, provided=False)],
        allow_missing_env=False,
        steps=[
            ApplyStep(kind="image", name="kind-registry:5000/legacy-flask-app:abc", action="push"),
            ApplyStep(kind="Namespace", name="gc-legacy-flask-app", action="create"),
        ],
        rendered="kind: Namespace\n",
        rendered_format="yaml",
        expected_url="http://legacy-flask-app.localhost",
    )
    checks = [
        PreflightCheck(name="image", status="pass", detail="Built and smoke-tested"),
        PreflightCheck(name="env", status="fail", detail="Missing required: DATABASE_URL"),
    ]
    card = deploy_plan_card(plan, checks)
    assert card.type == "deploy_plan" and card.source_image == plan.source_image_id
    assert [(r.kind, r.action) for r in card.resources] == [
        ("image", "push"),
        ("Namespace", "create"),
    ]
    assert card.preflight == checks and card.expected_url == plan.expected_url
    assert [c.name for c in blocked(checks)] == ["env"]
    overridden = [checks[0], checks[1].model_copy(update={"overridden": True})]
    assert blocked(overridden) == []
    assert plan_summary(plan) == "Deployment legacy-flask-app, 1 replica, 250m / 512Mi"
