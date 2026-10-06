"""Redact anything that looks like a secret, and truncate text for summaries and logs.

Used for run_steps summaries, error messages, and every log line. Patterns are
deliberately broad: a false positive costs a little readability, a miss leaks a secret.
"""

import re
from functools import lru_cache
from typing import Any

from app.config import get_settings

REDACTED = "[REDACTED]"
MAX_SUMMARY_CHARS = 2000

_KEY_WORDS = (
    r"password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|"
    r"private[_-]?key|client[_-]?secret|credential|authorization|auth"
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # PEM private keys (whole block, or to the end if truncated)
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
        REDACTED,
    ),
    # Credentials in URLs: scheme://user:password@host
    (re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:)[^@\s/]+(@)"), rf"\1{REDACTED}\2"),
    # Authorization headers
    (re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}"), rf"\1 {REDACTED}"),
    # Well-known token formats
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),
    # key=value / "key": "value" where the key names a secret. Pure numbers stay (max_tokens: 20).
    (
        re.compile(
            rf"(?i)(\b[\w.-]*(?:{_KEY_WORDS})[\w.-]*[\"']?\s*[:=]\s*[\"']?)"
            r"(?!\d+\b)[^\s\"',;}&]+"
        ),
        rf"\1{REDACTED}",
    ),
)


@lru_cache
def _configured_secrets() -> tuple[str, ...]:
    """Exact values of secrets we hold, so they are removed even if no pattern matches."""
    s = get_settings()
    values = (
        s.token_factory_api_key,
        s.tavily_api_key,
        s.secrets_key,
        s.database_url,
        s.github_app_client_secret,
        s.github_webhook_secret,
        s.dev_api_key,
    )
    found = (v.get_secret_value() for v in values if v is not None)
    # Very short values would redact ordinary words.
    return tuple(v for v in found if len(v) >= 8)


def redact(text: str) -> str:
    for secret in _configured_secrets():
        text = text.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def truncate(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    if len(text) <= limit:
        return text
    marker = f" ... [truncated, {len(text)} chars total]"
    return text[: max(limit - len(marker), 0)] + marker


def summarize(text: str | None, limit: int = MAX_SUMMARY_CHARS) -> str | None:
    """Redact, then truncate, so truncation cannot cut a secret so a pattern misses it."""
    if text is None:
        return None
    return truncate(redact(text), limit)


_SECRET_KEY_NAME = re.compile(rf"(?i)(?:{_KEY_WORDS})")


def redact_json(value: Any, key: str | None = None) -> Any:
    """Redact a JSON-like value for display. Values under secret-sounding keys are hidden."""
    if key is not None and _SECRET_KEY_NAME.search(key) and isinstance(value, str):
        return REDACTED
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_json(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_json(v) for v in value]
    return value
