"""Smoke test for NVIDIA Nemotron models on Nebius Token Factory.

Reads TOKEN_FACTORY_BASE_URL, TOKEN_FACTORY_API_KEY, MODEL_NANO, MODEL_SUPER, and MODEL_ULTRA
from the repo root .env (or the environment). Sends at most one tiny chat request per model.

Credits are limited: no loops over prompts, at most one retry, max_tokens=20.
Exits non-zero if any configured model fails. Never prints the API key.

Run: make models  (or: uv run --project apps/api python scripts/test_models.py)
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI, OpenAIError

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL_VARS = ("MODEL_NANO", "MODEL_SUPER", "MODEL_ULTRA")
PROMPT = "Reply with exactly: ok"
MAX_TOKENS = 20
TIMEOUT_SECONDS = 60.0


@dataclass
class Result:
    env_var: str
    model_id: str
    ok: bool
    detail: str
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


def redact(text: str, secret: str) -> str:
    """Remove the API key from any text before printing it."""
    return text.replace(secret, "***") if secret else text


def short_error(exc: Exception, secret: str) -> str:
    message = redact(f"{type(exc).__name__}: {exc}", secret)
    return message if len(message) <= 160 else message[:157] + "..."


def list_nvidia_models(client: OpenAI, secret: str) -> None:
    print("Step 1: listing models")
    try:
        ids = sorted(m.id for m in client.models.list())
    except OpenAIError as exc:
        print(f"  Model listing not available ({short_error(exc, secret)}). Continuing.\n")
        return
    matches = [i for i in ids if "nemotron" in i.lower() or "nvidia" in i.lower()]
    print(f"  {len(ids)} models available, {len(matches)} match 'nemotron' or 'nvidia':")
    for model_id in matches:
        print(f"    {model_id}")
    print()


def ping(client: OpenAI, env_var: str, model_id: str, secret: str) -> Result:
    start = time.perf_counter()
    try:
        response = client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": PROMPT}],
            max_tokens=MAX_TOKENS,
        )
    except OpenAIError as exc:
        latency_ms = round((time.perf_counter() - start) * 1000)
        detail = short_error(exc, secret)
        return Result(env_var, model_id, ok=False, detail=detail, latency_ms=latency_ms)
    latency_ms = round((time.perf_counter() - start) * 1000)
    usage = response.usage
    reply = (response.choices[0].message.content or "").strip() if response.choices else ""
    return Result(
        env_var,
        model_id,
        ok=True,
        detail=f"reply: {reply[:30]!r}",
        latency_ms=latency_ms,
        prompt_tokens=usage.prompt_tokens if usage else None,
        completion_tokens=usage.completion_tokens if usage else None,
    )


def print_table(results: list[Result]) -> None:
    headers = (
        "Var",
        "Model ID",
        "Result",
        "Latency ms",
        "Prompt tok",
        "Compl tok",
        "Detail",
    )
    rows = [
        (
            r.env_var,
            r.model_id,
            "success" if r.ok else "error",
            "-" if r.latency_ms is None else str(r.latency_ms),
            "-" if r.prompt_tokens is None else str(r.prompt_tokens),
            "-" if r.completion_tokens is None else str(r.completion_tokens),
            r.detail,
        )
        for r in results
    ]
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True))
    print(line)
    print("-" * len(line))
    for row in rows:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)))


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    base_url = os.getenv("TOKEN_FACTORY_BASE_URL", "").strip()
    api_key = os.getenv("TOKEN_FACTORY_API_KEY", "").strip()

    missing = [
        n
        for n, v in (
            ("TOKEN_FACTORY_BASE_URL", base_url),
            ("TOKEN_FACTORY_API_KEY", api_key),
        )
        if not v
    ]
    if missing:
        print(f"Missing required env vars: {', '.join(missing)}. Fill them in .env.")
        return 2

    models = [(var, os.getenv(var, "").strip()) for var in MODEL_VARS]
    configured = [(var, mid) for var, mid in models if mid]
    for var, mid in models:
        if not mid:
            print(f"{var} is not set, skipping.")

    client = OpenAI(base_url=base_url, api_key=api_key, max_retries=1, timeout=TIMEOUT_SECONDS)
    print(f"Token Factory base URL: {base_url}\n")

    list_nvidia_models(client, api_key)

    if not configured:
        print("No MODEL_NANO, MODEL_SUPER, or MODEL_ULTRA set. Nothing to test.")
        return 1

    print(f"Step 2: one chat request per model ({PROMPT!r}, max_tokens={MAX_TOKENS})\n")
    results = [ping(client, var, mid, api_key) for var, mid in configured]
    print_table(results)

    failed = [r for r in results if not r.ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} configured models succeeded.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
