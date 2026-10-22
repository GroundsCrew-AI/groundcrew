import json
import logging

import pytest

from app.log import JsonFormatter
from app.security.redact import REDACTED, redact, redact_json, summarize, truncate


@pytest.mark.parametrize(
    "secret_text",
    [
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
        "key sk-proj-1234567890abcdefghijkl",
        "aws AKIAIOSFODNN7EXAMPLE",
        "gh ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
        "postgresql://user:hunter2pass@db.example.com/app",
        '{"api_key": "abc123def456"}',
        "password=correct-horse-battery",
        "client_secret: s3cr3tvalue",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
    ],
)
def test_redacts_secret_shapes(secret_text: str) -> None:
    out = redact(secret_text)
    assert REDACTED in out
    for fragment in (
        "abcdefghijklmnopqrstuvwxyz123456",
        "1234567890abcdefghijkl",
        "IOSFODNN7EXAMPLE",
        "hunter2pass",
        "abc123def456",
        "correct-horse",
        "s3cr3tvalue",
        "MIIEpAIBAAKCAQEA",
    ):
        assert fragment not in out


def test_keeps_ordinary_text() -> None:
    text = "Listed 3 services: api, worker, web. max_tokens: 20, prompt_tokens=120"
    assert redact(text) == text


def test_keeps_secrets_manager_action_names_but_not_values() -> None:
    text = "may not use secretsmanager:DescribeSecret or secretsmanager:UpdateSecretVersionStage"
    assert redact(text) == text
    assert redact("secret: Hunter2Password") == f"secret: {REDACTED}"
    assert redact("secretsmanager:hunter2") == f"secretsmanager:{REDACTED}"


def test_redacts_configured_secret_value() -> None:
    # conftest sets TOKEN_FACTORY_API_KEY to this value.
    assert "test-key-not-real" not in redact("called with test-key-not-real")


def test_truncate_to_limit() -> None:
    out = truncate("x" * 5000, 2000)
    assert len(out) == 2000
    assert out.endswith("[truncated, 5000 chars total]")
    assert truncate("short") == "short"


def test_summarize_redacts_before_truncating() -> None:
    out = summarize("password=" + "p" * 3000)
    assert out is not None and "ppp" not in out and len(out) <= 2000
    assert summarize(None) is None


def test_json_log_lines_are_redacted() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "token=%s", ("abcdefgh12345",), None)
    record.kind = "run_agent"
    line = json.loads(JsonFormatter().format(record))
    assert "abcdefgh12345" not in line["msg"]
    assert line["kind"] == "run_agent"
    assert line["level"] == "INFO"


def test_json_log_extra_fields_are_redacted() -> None:
    record = logging.LogRecord("t", logging.INFO, "f", 1, "llm call", (), None)
    record.error = "Bearer abcdefghijklmnop failed"
    record.prompt_tokens = 21
    entry = json.loads(JsonFormatter().format(record))
    assert "abcdefghijklmnop" not in entry["error"]
    assert entry["prompt_tokens"] == 21


def test_redact_json_hides_secret_keys_and_values() -> None:
    from app.security.redact import redact_json

    value = {"name": "api", "password": "p", "nested": [{"token": "t0k3n"}, "Bearer abcdefgh123"]}
    out = redact_json(value)
    assert out["name"] == "api" and out["password"] == REDACTED
    assert out["nested"][0]["token"] == REDACTED
    assert "abcdefgh123" not in out["nested"][1]


def test_arns_and_secret_identifiers_are_kept() -> None:
    arn = "arn:aws:secretsmanager:us-east-2:881050425883:secret:checkout/database-url-AbCdEf"
    assert (
        redact(f"unable to pull secrets: {arn} failed") == f"unable to pull secrets: {arn} failed"
    )
    assert redact("secret_id=checkout/database-url password=hunter2") == (
        f"secret_id=checkout/database-url password={REDACTED}"
    )
    assert redact('{"SecretName": "checkout/database-url", "secret": "abc123xyz"}') == (
        f'{{"SecretName": "checkout/database-url", "secret": "{REDACTED}"}}'
    )
    assert redact_json({"SecretId": "checkout/db", "client_secret": "x"}) == {
        "SecretId": "checkout/db",
        "client_secret": REDACTED,
    }
    # An ARN does not shield what follows it.
    assert "hunter2" not in redact("arn:aws:s3:::bucket?password=hunter2")
