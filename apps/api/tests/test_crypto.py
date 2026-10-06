import pytest
from pydantic import SecretStr

from app.config import Settings
from app.security import crypto


def test_round_trip() -> None:
    key = crypto.generate_key()
    token = crypto.encrypt("kubeconfig: secret-value", key=key)
    assert "secret-value" not in token
    assert crypto.decrypt(token, key=key) == "kubeconfig: secret-value"


def test_wrong_key_fails() -> None:
    token = crypto.encrypt("ssh-private-key", key=crypto.generate_key())
    with pytest.raises(crypto.DecryptionError) as exc_info:
        crypto.decrypt(token, key=crypto.generate_key())
    assert "ssh-private-key" not in str(exc_info.value)


def test_tampered_token_fails() -> None:
    key = crypto.generate_key()
    token = crypto.encrypt("value", key=key)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(token[:-4] + "AAAA", key=key)


def test_missing_key_has_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crypto, "get_settings", lambda: Settings(_env_file=None, secrets_key=None))
    with pytest.raises(crypto.SecretsKeyError, match="SECRETS_KEY is not set"):
        crypto.encrypt("value")


def test_invalid_key_has_clear_error() -> None:
    with pytest.raises(crypto.SecretsKeyError, match="not a valid Fernet key"):
        crypto.encrypt("value", key="not-a-key")


def test_uses_secrets_key_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    key = crypto.generate_key()
    settings = Settings(_env_file=None, secrets_key=SecretStr(key))
    monkeypatch.setattr(crypto, "get_settings", lambda: settings)
    assert crypto.decrypt(crypto.encrypt("value")) == "value"
    assert crypto.decrypt(crypto.encrypt("value"), key=key) == "value"
