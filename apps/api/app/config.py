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
    model_embed: str | None = None

    # LLM calls
    llm_timeout_seconds: float = 60.0
    llm_max_tokens: int = 4096
    # A call is not made if budgets leave less than this for the completion (reasoning included)
    llm_min_completion_tokens: int = 512
    # Tiers that reason before answering (comma separated). Others send enable_thinking=false,
    # which cut a tiny test reply from 20+ tokens to 2 on all three Nemotron models.
    llm_reasoning_tiers: str = "ultra"
    # Use Super wherever Ultra would be used (Ultra credits are limited)
    dev_mode_no_ultra: bool = False

    # Optional prices in USD per million tokens. Unset means cost is not estimated.
    price_nano_input_per_m: float | None = None
    price_nano_output_per_m: float | None = None
    price_super_input_per_m: float | None = None
    price_super_output_per_m: float | None = None
    price_ultra_input_per_m: float | None = None
    price_ultra_output_per_m: float | None = None

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

    # Budgets and agent limits. Unset daily budget or Ultra limit means no limit.
    daily_token_budget: int | None = None
    max_ultra_calls_per_incident: int | None = None
    max_steps_per_run: int = 12
    max_tokens_per_run: int = 60_000

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
