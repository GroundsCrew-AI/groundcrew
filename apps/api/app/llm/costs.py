"""Cost estimate per LLM call from optional PRICE_<TIER>_INPUT/OUTPUT_PER_M settings."""

from decimal import Decimal

from app.config import Settings, get_settings
from app.llm.router import Tier

_MILLION = Decimal(1_000_000)
_PLACES = Decimal("0.000001")


def estimate_cost(
    tier: Tier,
    prompt_tokens: int,
    completion_tokens: int,
    settings: Settings | None = None,
) -> Decimal | None:
    """USD cost, or None when the tier's input or output price is not set."""
    settings = settings or get_settings()
    input_price: float | None = getattr(settings, f"price_{tier}_input_per_m")
    output_price: float | None = getattr(settings, f"price_{tier}_output_per_m")
    if input_price is None or output_price is None:
        return None
    cost = (
        Decimal(prompt_tokens) * Decimal(str(input_price))
        + Decimal(completion_tokens) * Decimal(str(output_price))
    ) / _MILLION
    return cost.quantize(_PLACES)
