"""Routes each task to a model tier, a reasoning mode, and a token cap.

Two dials, not one: the tier picks the model size, the reasoning mode picks how much it
thinks. Most agent-loop steps are "call the next tool" and need no thinking; only diagnosis
and fix design get full reasoning.
"""

import enum
from dataclasses import dataclass, replace

from app.config import Settings, get_settings
from app.llm.models import (
    ModelNotConfiguredError,
    Reasoning,
    Tier,
    model_id_for,
    next_tier,
)

__all__ = [
    "ModelNotConfiguredError",
    "Reasoning",
    "Route",
    "Task",
    "TaskType",
    "Tier",
    "lightning_enabled",
    "route",
]


class Task(enum.StrEnum):
    # Scout (Nano)
    summarize_graph = "summarize_graph"
    detect_app_spec = "detect_app_spec"
    compress_evidence = "compress_evidence"  # map step over log/metric chunks
    classify_signal = "classify_signal"
    # Builder / Deployer (Super)
    write_dockerfile = "write_dockerfile"
    fix_build = "fix_build"
    plan_deploy = "plan_deploy"
    tool_step = "tool_step"  # generic "pick the next tool" step
    # Medic (Ultra for thinking, Super for legwork)
    investigate_step = "investigate_step"
    root_cause = "root_cause"
    # A close past incident was found: check its diagnosis against this incident's evidence.
    confirm_root_cause = "confirm_root_cause"
    propose_fix = "propose_fix"
    verify_recovery = "verify_recovery"
    # Generic task types from Phase 1 (Echo Ops and other tool-loop agents)
    classify = "classify"
    summarize = "summarize"
    detect = "detect"
    write_config = "write_config"
    plan = "plan"
    patch = "patch"


# Phase 1 name, kept so existing agents and imports work.
TaskType = Task


@dataclass(frozen=True)
class Route:
    task: Task
    tier: Tier
    reasoning: Reasoning
    # Completion cap for one call, reasoning included. None = LLM_MAX_TOKENS.
    max_tokens: int | None
    # NVIDIA's recommended sampling for Nemotron 3, tool calling included.
    temperature: float = 1.0
    top_p: float = 0.95
    # Tier to try if the primary model fails with a retryable error or a 5xx.
    fallback: Tier | None = None
    escalated: bool = False


_N, _S, _U = Tier.nano, Tier.super, Tier.ultra
_OFF, _LOW, _ON = Reasoning.off, Reasoning.low, Reasoning.on

ROUTES: dict[Task, Route] = {
    Task.summarize_graph: Route(Task.summarize_graph, _N, _OFF, 1_500),
    Task.detect_app_spec: Route(Task.detect_app_spec, _N, _LOW, 2_000, fallback=_S),
    Task.compress_evidence: Route(Task.compress_evidence, _N, _OFF, 1_200),
    Task.classify_signal: Route(Task.classify_signal, _N, _OFF, 300),
    Task.write_dockerfile: Route(Task.write_dockerfile, _S, _LOW, 3_000),
    Task.fix_build: Route(Task.fix_build, _S, _ON, 6_000),
    Task.plan_deploy: Route(Task.plan_deploy, _S, _LOW, 3_000),
    Task.tool_step: Route(Task.tool_step, _S, _OFF, 1_500, fallback=_N),
    Task.investigate_step: Route(Task.investigate_step, _S, _OFF, 1_500),
    Task.root_cause: Route(Task.root_cause, _U, _ON, 10_000, fallback=_S),
    Task.confirm_root_cause: Route(Task.confirm_root_cause, _S, _LOW, 4_000),
    # Ultra and Lightning never run with reasoning=low: in the probes it saved little or cost
    # more (Ultra 311 vs 362 tokens over 5 samples, wide spread; Lightning 603 vs 460).
    Task.propose_fix: Route(Task.propose_fix, _U, _ON, 6_000, fallback=_S),
    Task.verify_recovery: Route(Task.verify_recovery, _N, _OFF, 800),
    # Phase 1 generic types: reasoning off (verified on Echo Ops: tool calls dropped from
    # 700 to 950 completion tokens to under 30), LLM_MAX_TOKENS per call.
    Task.classify: Route(Task.classify, _N, _OFF, None),
    Task.summarize: Route(Task.summarize, _N, _OFF, None),
    Task.detect: Route(Task.detect, _N, _OFF, None),
    Task.write_config: Route(Task.write_config, _S, _OFF, None),
    Task.plan: Route(Task.plan, _S, _OFF, None),
    Task.patch: Route(Task.patch, _S, _OFF, None),
}


# Tasks that move to Nemotron 3.5 Lightning once it has a price (lightning_enabled). Until
# then they keep their ROUTES entry. Probe: Lightning ran a 3-turn tool loop exactly right at
# Nano-like latency (about 270 ms) and read 60k-token prompts correctly.
LIGHTNING_ROUTES: dict[Task, Route] = {
    Task.tool_step: Route(Task.tool_step, Tier.lightning, _OFF, 1_500, fallback=_S),
    Task.investigate_step: Route(Task.investigate_step, Tier.lightning, _OFF, 1_500, fallback=_S),
    Task.verify_recovery: Route(Task.verify_recovery, Tier.lightning, _OFF, 800, fallback=_N),
}

# Tiers for which reasoning=low costs more than on (probes): low is sent as on instead.
_NO_LOW_TIERS = frozenset({Tier.ultra, Tier.lightning})


def lightning_enabled(settings: Settings) -> bool:
    """Lightning routes are on when Lightning has a model ID and both prices."""
    return bool(
        settings.model_lightning
        and settings.price_lightning_input_per_m is not None
        and settings.price_lightning_output_per_m is not None
    )


def _no_ultra(tier: Tier | None) -> Tier | None:
    return Tier.super if tier == Tier.ultra else tier


def route(task: Task, *, escalate: bool = False, settings: Settings | None = None) -> Route:
    """The route for a task. `escalate=True` bumps one tier and turns reasoning fully on, with
    1.5x the token cap and the original tier as the fallback.

    Use escalation when a cheaper attempt already failed (Builder's 3rd failed build moves from
    Super to Ultra). DEV_MODE_NO_ULTRA keeps everything on Super or below.
    """
    settings = settings or get_settings()
    r = ROUTES[task]
    if task in LIGHTNING_ROUTES and lightning_enabled(settings):
        r = LIGHTNING_ROUTES[task]
    if escalate:
        cap = int((r.max_tokens or settings.llm_max_tokens) * 1.5)
        r = replace(
            r,
            tier=next_tier(r.tier),
            reasoning=Reasoning.on,
            max_tokens=cap,
            fallback=r.tier,
            escalated=True,
        )
    if settings.dev_mode_no_ultra:
        tier = _no_ultra(r.tier) or r.tier
        fallback = _no_ultra(r.fallback)
        r = replace(r, tier=tier, fallback=None if fallback == tier else fallback)
    if r.tier in _NO_LOW_TIERS and r.reasoning == Reasoning.low:
        r = replace(r, reasoning=Reasoning.on)  # low costs more than on for these tiers
    return r


def model_id(task: Task, settings: Settings | None = None) -> str:
    """The model a task uses (no escalation). Convenience for scripts and checks."""
    settings = settings or get_settings()
    return model_id_for(route(task, settings=settings).tier, settings)
