"""Join tokens and signed agent requests.

A join token (`gcj_...`) is shown once to the owner who adds the agent; only its SHA-256 is
stored, and it works once, until it expires. On enrollment the agent sends its Ed25519 public key;
the private key stays on the host.

Every later request carries four headers and is signed over a canonical text, so neither a
changed body nor a replayed request passes:

    X-Groundcrew-Agent:      the agent id
    X-Groundcrew-Timestamp:  Unix seconds; refused when off by more than AGENT_REQUEST_SKEW_SECONDS
    X-Groundcrew-Nonce:      16 to 64 URL-safe characters, used once per agent
    X-Groundcrew-Signature:  base64 Ed25519 signature of these lines, joined by newlines:

    groundcrew-agent-v1
    <METHOD>
    <path with query>
    <timestamp>
    <nonce>
    <sha256 hex of the body>

The Go agent builds the same text (apps/agent/internal/client/sign.go).
"""

import base64
import binascii
import hashlib
import re
import secrets

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

TOKEN_PREFIX = "gcj_"  # noqa: S105 - a prefix, not a secret
SIGNATURE_VERSION = "groundcrew-agent-v1"
NONCE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class SignatureError(Exception):
    """A request that is not validly signed by the agent it names."""


def new_join_token() -> tuple[str, str]:
    """A join token and the hash to store."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def public_key(value: str) -> str:
    """The base64 of a raw 32-byte Ed25519 public key, checked; raises ValueError."""
    try:
        raw = base64.b64decode(value, validate=True)
        Ed25519PublicKey.from_public_bytes(raw)
    except (binascii.Error, ValueError):
        raise ValueError("Not a base64 Ed25519 public key.") from None
    return base64.b64encode(raw).decode()


def canonical(method: str, path: str, timestamp: str, nonce: str, body: bytes) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return "\n".join((SIGNATURE_VERSION, method.upper(), path, timestamp, nonce, digest)).encode()


def verify(key: str, signature: str, message: bytes) -> None:
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(key)).verify(
            base64.b64decode(signature, validate=True), message
        )
    except (InvalidSignature, binascii.Error, ValueError):
        raise SignatureError("The request signature does not match this agent's key.") from None
