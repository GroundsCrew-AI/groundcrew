"""Application settings loaded from environment variables and .env.

Secrets are optional at import time so the app (and /health) can start without them.
Code that needs a secret must check for it and fail with a clear error at call time.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root .env (apps/api/app/config.py -> ../../../.env), then a local .env if present.
_REPO_ROOT_ENV = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_REPO_ROOT_ENV, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Nebius Token Factory
    token_factory_base_url: str | None = None
    token_factory_api_key: SecretStr | None = None

    # Model IDs (exact IDs come from the Token Factory docs)
    model_nano: str | None = None
    model_super: str | None = None
    model_ultra: str | None = None
    model_embed: str | None = None

    # External services
    tavily_api_key: SecretStr | None = None
    database_url: SecretStr | None = None
    secrets_key: SecretStr | None = None

    # GitHub App
    github_app_id: str | None = None
    github_app_private_key: SecretStr | None = None

    # Budgets
    daily_token_budget: int | None = None
    max_ultra_calls_per_incident: int | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
