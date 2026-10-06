"""Map task types to model tiers, and tiers to model IDs from settings."""

import enum
from dataclasses import dataclass

from app.config import Settings, get_settings


class Tier(enum.StrEnum):
    nano = "nano"
    super = "super"
    ultra = "ultra"


class TaskType(enum.StrEnum):
    classify = "classify"
    summarize = "summarize"
    detect = "detect"
    write_config = "write_config"
    plan = "plan"
    patch = "patch"
    root_cause = "root_cause"


TASK_TIERS: dict[TaskType, Tier] = {
    TaskType.classify: Tier.nano,
    TaskType.summarize: Tier.nano,
    TaskType.detect: Tier.nano,
    TaskType.write_config: Tier.super,
    TaskType.plan: Tier.super,
    TaskType.patch: Tier.super,
    TaskType.root_cause: Tier.ultra,
}


class ModelNotConfiguredError(RuntimeError):
    pass


@dataclass(frozen=True)
class Route:
    task_type: TaskType
    tier: Tier
    model_id: str


def model_id_for(tier: Tier, settings: Settings) -> str:
    model_id = {
        Tier.nano: settings.model_nano,
        Tier.super: settings.model_super,
        Tier.ultra: settings.model_ultra,
    }[tier]
    if not model_id:
        raise ModelNotConfiguredError(f"MODEL_{tier.upper()} is not set. See .env.example.")
    return model_id


def route(task_type: TaskType, settings: Settings | None = None) -> Route:
    settings = settings or get_settings()
    tier = TASK_TIERS[task_type]
    if tier == Tier.ultra and settings.dev_mode_no_ultra:
        tier = Tier.super
    return Route(task_type=task_type, tier=tier, model_id=model_id_for(tier, settings))
