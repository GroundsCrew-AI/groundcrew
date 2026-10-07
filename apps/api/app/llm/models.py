"""Model registry for Nebius Token Factory: tiers, reasoning modes, prices, context sizes.

Every value comes from settings (.env). Run scripts/probe_token_factory.py once and copy the
confirmed model IDs and context sizes into .env. Default prices are UNVERIFIED (third-party
listing); set the real ones from the Token Factory pricing page. A model with no price is never
called, so every call's spend is counted.
"""

import enum
from dataclasses import dataclass
from decimal import Decimal

from app.config import Settings


class Tier(enum.StrEnum):
    nano = "nano"
    super = "super"
    ultra = "ultra"


class Reasoning(enum.StrEnum):
    off = "off"  # enable_thinking=false: no thinking tokens, fastest and cheapest
    low = "low"  # enable_thinking=true, low_effort=true: short thinking
    on = "on"  # full reasoning


TIER_ORDER = (Tier.nano, Tier.super, Tier.ultra)

_MILLION = Decimal(1_000_000)
_PLACES = Decimal("0.000001")


class ModelNotConfiguredError(RuntimeError):
    pass


class PriceMissingError(ModelNotConfiguredError):
    """A model has no price. We refuse to call it rather than spend without counting."""


def next_tier(tier: Tier) -> Tier:
    index = TIER_ORDER.index(tier)
    return TIER_ORDER[min(index + 1, len(TIER_ORDER) - 1)]


def usd(tokens: int, price_per_m: float) -> Decimal:
    return (Decimal(tokens) * Decimal(str(price_per_m)) / _MILLION).quantize(_PLACES)


@dataclass(frozen=True)
class ModelSpec:
    tier: Tier
    model_id: str
    usd_per_m_in: float
    usd_per_m_out: float
    max_context: int

    def cost(self, prompt_tokens: int, completion_tokens: int) -> Decimal:
        return usd(prompt_tokens, self.usd_per_m_in) + usd(completion_tokens, self.usd_per_m_out)


def model_id_for(tier: Tier, settings: Settings) -> str:
    model_id: str | None = getattr(settings, f"model_{tier}")
    if not model_id:
        raise ModelNotConfiguredError(f"MODEL_{tier.upper()} is not set. See .env.example.")
    return model_id


def model_spec(tier: Tier, settings: Settings) -> ModelSpec:
    price_in: float | None = getattr(settings, f"price_{tier}_input_per_m")
    price_out: float | None = getattr(settings, f"price_{tier}_output_per_m")
    if price_in is None or price_out is None:
        raise PriceMissingError(
            f"PRICE_{tier.upper()}_INPUT_PER_M and PRICE_{tier.upper()}_OUTPUT_PER_M must be set. "
            "Refusing to call a model whose spend cannot be counted."
        )
    return ModelSpec(
        tier=tier,
        model_id=model_id_for(tier, settings),
        usd_per_m_in=price_in,
        usd_per_m_out=price_out,
        max_context=getattr(settings, f"llm_context_{tier}"),
    )


def ultra_equivalent(prompt_tokens: int, completion_tokens: int, settings: Settings) -> Decimal:
    """What the same tokens would cost on Ultra. Needs only Ultra's prices, not its model ID."""
    price_in = settings.price_ultra_input_per_m
    price_out = settings.price_ultra_output_per_m
    if price_in is None or price_out is None:
        raise PriceMissingError("PRICE_ULTRA_* must be set to report the Ultra-equivalent cost.")
    return usd(prompt_tokens, price_in) + usd(completion_tokens, price_out)
