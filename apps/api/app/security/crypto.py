"""Fernet encryption for stored secrets (kubeconfigs, SSH keys, tokens).

The key comes from SECRETS_KEY. Generate one with `make keygen`.
Error messages never include the key or the plaintext.
"""

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class SecretsKeyError(RuntimeError):
    """SECRETS_KEY is missing or not a valid Fernet key."""


class DecryptionError(ValueError):
    """The token was not made with this key, or it was changed."""


def generate_key() -> str:
    return Fernet.generate_key().decode()


def _fernet(key: str | None) -> Fernet:
    if key is None:
        secret = get_settings().secrets_key
        key = secret.get_secret_value() if secret is not None else None
    if not key:
        raise SecretsKeyError(
            "SECRETS_KEY is not set. Generate one with `make keygen` and add it to .env."
        )
    try:
        return Fernet(key.encode())
    except ValueError:
        raise SecretsKeyError(
            "SECRETS_KEY is not a valid Fernet key (32 url-safe base64 bytes). "
            "Generate one with `make keygen`."
        ) from None


def encrypt(plaintext: str, *, key: str | None = None) -> str:
    """Encrypt text. Uses SECRETS_KEY unless a key is passed."""
    return _fernet(key).encrypt(plaintext.encode()).decode()


def decrypt(token: str, *, key: str | None = None) -> str:
    """Decrypt a token from `encrypt`. Raises DecryptionError on a wrong key or bad token."""
    try:
        return _fernet(key).decrypt(token.encode()).decode()
    except InvalidToken:
        raise DecryptionError(
            "Could not decrypt secret: wrong SECRETS_KEY or corrupted data."
        ) from None
