"""Password hashing with scrypt (standard library, no extra dependency).

Stored as "scrypt$<log2 n>$<r>$<p>$<salt b64>$<hash b64>", so the cost can be raised later:
`needs_rehash` tells sign-in to store a new hash at the current cost. Hashing takes tens of
milliseconds on purpose; call the async helpers so the event loop keeps serving requests.
"""

import asyncio
import base64
import hashlib
import hmac
import secrets

LOG2_N = 15
R = 8
P = 1
DKLEN = 32
SALT_BYTES = 16
# n=2**15, r=8 needs 32 MiB; allow some headroom.
_MAXMEM = 64 * 1024 * 1024
MAX_LENGTH = 200

# A short list of passwords people pick first; the length rule removes most of the rest.
_COMMON = frozenset(
    """
    password1234 password123 1234567890 12345678910 qwertyuiop qwerty12345 iloveyou123
    passw0rd123 administrator letmein1234 welcome1234 groundcrew1 groundcrew123 0987654321
    1q2w3e4r5t abc1234567 changeme123 password12 password!1 qwerty1234 1111111111 0000000000
    """.split()  # noqa: SIM905
)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4))


def _derive(password: str, salt: bytes, log2_n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=2**log2_n, r=r, p=p, dklen=DKLEN, maxmem=_MAXMEM
    )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _derive(password, salt, LOG2_N, R, P)
    return f"scrypt${LOG2_N}${R}${P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    """Whether `password` matches. A missing or malformed hash never matches."""
    if not stored or len(password) > MAX_LENGTH:
        return False
    try:
        scheme, log2_n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = _unb64(digest)
        actual = _derive(password, _unb64(salt), int(log2_n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str) -> bool:
    return not stored.startswith(f"scrypt${LOG2_N}${R}${P}$")


# A hash of nothing in particular: sign-in for an unknown email still spends the same time,
# so response times do not tell which addresses have accounts.
_DUMMY = hash_password(secrets.token_urlsafe(16))


async def hash_password_async(password: str) -> str:
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, stored: str | None) -> bool:
    return await asyncio.to_thread(verify_password, password, stored or _DUMMY)


def password_problem(password: str, *, min_length: int, email: str | None = None) -> str | None:
    """Why this password cannot be used, or None."""
    if len(password) < min_length:
        return f"Use at least {min_length} characters."
    if len(password) > MAX_LENGTH:
        return f"Use at most {MAX_LENGTH} characters."
    lowered = password.lower()
    if lowered in _COMMON or len(set(password)) < 4:
        return "That password is too easy to guess. Try a few unrelated words."
    if email and lowered == email.lower():
        return "Do not use your email address as the password."
    return None
