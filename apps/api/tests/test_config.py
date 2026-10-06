import pytest

from app.config import Settings


def test_settings_load_without_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOKEN_FACTORY_API_KEY", raising=False)
    settings = Settings(_env_file=None)
    assert settings.token_factory_api_key is None


def test_secret_is_not_exposed_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN_FACTORY_API_KEY", "sk-test-value")
    settings = Settings(_env_file=None)
    assert "sk-test-value" not in repr(settings)
