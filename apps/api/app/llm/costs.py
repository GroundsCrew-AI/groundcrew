"""Cost per call from the PRICE_<TIER>_INPUT/OUTPUT_PER_M settings.

The client prices every call through app.llm.models (and refuses models with no price);
these helpers are for scripts and reports that only have token counts.
"""

from decimal import Decimal

from app.config import Settings, get_settings
from app.llm.models import Tier, ultra_equivalent, usd

__all__ = ["estimate_cost", "ultra_equivalent"]


def estimate_cost(
    tier: Tier,
    prompt_tokens: int,
    completion_tokens: int,
    settings: Settings | None = None,
) -> Decimal | None:
    """USD cost, or None when the tier's input or output price is not set."""
    settings = settings or get_settings()
    price_in: float | None = getattr(settings, f"price_{tier}_input_per_m")
    price_out: float | None = getattr(settings, f"price_{tier}_output_per_m")
    if price_in is None or price_out is None:
        return None
    return usd(prompt_tokens, price_in) + usd(completion_tokens, price_out)
