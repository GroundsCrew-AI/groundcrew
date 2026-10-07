"""Application settings loaded from environment variables and .env.

Secrets are optional at import time so the app (and /health) can start without them.
Code that needs a secret must check for it and fail with a clear error at call time.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root .env (apps/api/app/config.py -> ../../../.env), then a local .env if present.
# In the Docker image the file is /app/app/config.py (no repo root); compose passes env vars.
_HERE = Path(__file__).resolve()
_REPO_ROOT_ENV = _HERE.parents[3] / ".env" if len(_HERE.parents) > 3 else Path(".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_REPO_ROOT_ENV, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        # An empty value in .env means "not set", so optional numbers can stay blank.
        env_ignore_empty=True,
    )

    # Nebius Token Factory
    token_factory_base_url: str | None = None
    token_factory_api_key: SecretStr | None = None

    # Model IDs (exact IDs come from the Token Factory docs)
    model_nano: str | None = None
    model_super: str | None = None
    model_ultra: str | None = None
    # Nemotron 3.5 Lightning (30B MoE, 3B active, built for tool-call execution). Its routes
    # (tool_step, investigate_step, verify_recovery) switch on once PRICE_LIGHTNING_* are set.
    model_lightning: str | None = "nvidia/Nemotron-3_5-Lightning"
    # The only embedding model on Token Factory (probe, 2026-10-07). It returns 4096 dimensions;
    # embed() asks for EMBED_DIM, else truncates and re-normalises (it is a Matryoshka model).
    model_embed: str | None = "Qwen/Qwen3-Embedding-8B"
    # Reranker on Token Factory (POST /v1/rerank). Unset = rerank keeps the input order.
    model_rerank: str | None = None
    # Usable context per tier, in tokens: the largest prompt the tier reads reliably, not the
    # largest it accepts. A bigger prompt goes to the route's fallback tier. Probe (2026-10-07):
    # Nano accepted 65k tokens but answered wrong from about 20k, right at 6k; Lightning was
    # right at 60k.
    llm_context_nano: int = 8_000
    llm_context_super: int = 256_000
    llm_context_ultra: int = 256_000
    llm_context_lightning: int = 64_000

    # LLM calls
    llm_timeout_seconds: float = 120.0
    # Tries per model for 429, 5xx, and connection errors (a timeout is retried once at most,
    # because a timed-out request may still be billed). Backoff: base * 2**n plus jitter.
    llm_max_attempts: int = 3
    llm_retry_base_seconds: float = 1.0
    # Max completion tokens for agent-loop steps on the generic task types (Echo Ops).
    # Pipeline tasks (Scout, Builder, Deployer) set their own limit in app/llm/router.py.
    llm_max_tokens: int = 4096
    # A call is not made if budgets leave less than this for the completion (reasoning included)
    llm_min_completion_tokens: int = 512
    # Use Super wherever Ultra would be used (Ultra credits are limited)
    dev_mode_no_ultra: bool = False

    # Prices in USD per million tokens. Defaults are from a third-party listing (Oct 2026),
    # UNVERIFIED: set the real ones from the Token Factory pricing page. A model with no price
    # is never called, so spend is always counted.
    price_nano_input_per_m: float | None = 0.06
    price_nano_output_per_m: float | None = 0.24
    price_super_input_per_m: float | None = 0.30
    price_super_output_per_m: float | None = 0.90
    price_ultra_input_per_m: float | None = 1.00
    price_ultra_output_per_m: float | None = 3.00
    # No public price seen for Lightning yet: it is refused, and its routes stay off, until set.
    price_lightning_input_per_m: float | None = None
    price_lightning_output_per_m: float | None = None
    # Embedding and rerank prices per million input tokens. No default: unset means not called.
    price_embed_per_m: float | None = None
    price_rerank_per_m: float | None = None

    # External services
    tavily_api_key: SecretStr | None = None
    database_url: SecretStr | None = None
    secrets_key: SecretStr | None = None

    # Vector size of incidents.embedding. Changing it needs a new migration.
    embed_dim: int = 1024

    # GitHub App
    github_app_id: str | None = None
    github_app_client_id: str | None = None
    github_app_client_secret: SecretStr | None = None
    github_app_private_key_path: Path | None = None
    github_webhook_secret: SecretStr | None = None

    # Spend caps in USD, counted in the llm_usage table. The total cap is the hard stop for the
    # whole hackathon (credits are about $30). Hosted demo: set LLM_DAILY_USD_CAP=0.50.
    llm_run_usd_cap: float = 0.40
    llm_daily_usd_cap: float = 2.00
    llm_total_usd_cap: float = 25.0

    # Token and call limits. Unset daily token budget or per-incident Ultra limit = no limit.
    daily_token_budget: int | None = None
    max_ultra_calls_per_incident: int | None = None
    max_ultra_calls_per_run: int = 3
    max_steps_per_run: int = 12
    max_tokens_per_run: int = 400_000

    # Worker and live updates
    worker_poll_interval_seconds: float = 1.0
    job_lock_timeout_seconds: int = 600
    # On SIGTERM, how long the current job may keep running before it is put back in the queue
    worker_shutdown_grace_seconds: float = 20.0
    sse_poll_interval_ms: int = 500

    # Auth. The web app signs a short-lived HS256 JWT with this secret for every API call
    # (and the API signs 60-second SSE stream tokens with it). At least 32 characters.
    internal_jwt_secret: SecretStr | None = None
    # The web app's origin. CORS is allowed for it only, for the direct SSE connection.
    web_origin: str = "http://localhost:3000"

    # Logging
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
