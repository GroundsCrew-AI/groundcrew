"""Day-1 probe for Nebius Token Factory. Costs well under $0.10 (about 20 small calls).

Run (you, not CI):  uv run --project apps/api python scripts/probe_token_factory.py

Reads TOKEN_FACTORY_BASE_URL, TOKEN_FACTORY_API_KEY, and MODEL_NANO/SUPER/ULTRA from the repo
root .env (or the environment). If a MODEL_* is blank it picks the Nemotron model from
/v1/models. Answers every assumption in apps/api/app/llm/models.py and router.py:

- exact Nemotron model IDs, and which embedding / rerank models exist (and their dimensions)
- does each model honour enable_thinking and low_effort (compare completion tokens off/low/on)
- where the reasoning trace comes back (reasoning_content, reasoning, or <think> in content)
- does tool calling work with reasoning on and off
- does response_format json_schema (strict) work per model

Writes probe_results.json in the current directory (git-ignored). Never prints the API key.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from openai import OpenAI

REPO_ROOT = Path(__file__).resolve().parents[1]
MODES: dict[str, dict[str, bool]] = {
    "off": {"enable_thinking": False},
    "low": {"enable_thinking": True, "low_effort": True},
    "on": {"enable_thinking": True},
}
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_ecs_service",
            "description": "Describe an AWS ECS service",
            "parameters": {
                "type": "object",
                "properties": {"cluster": {"type": "string"}, "service": {"type": "string"}},
                "required": ["cluster", "service"],
            },
        },
    }
]
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "language": {"type": "string"},
        "port": {"type": "integer"},
        "needs_database": {"type": "boolean"},
    },
    "required": ["language", "port", "needs_database"],
    "additionalProperties": False,
}
QUESTION = [
    {
        "role": "user",
        "content": "A container exits with code 137 right after a deploy that changed its task "
        "definition. Most likely cause, one sentence.",
    }
]
TOOL_QUESTION = [{"role": "user", "content": "Check the 'worker' service in cluster 'demo'."}]
SCHEMA_QUESTION = [
    {
        "role": "user",
        "content": "Repo has package.json with next, a .env.example with DATABASE_URL, and "
        "'next start -p 3000'. Give the app spec as JSON.",
    }
]


def redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def pick(nemotron: list[str], *needles: str) -> str | None:
    for model_id in nemotron:
        if all(n in model_id.lower() for n in needles):
            return model_id
    return None


def call(client: OpenAI, key: str, model: str, mode: str, **kwargs: Any) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        response = client.chat.completions.create(
            model=model,
            max_tokens=kwargs.pop("max_tokens", 1200),
            temperature=1.0,
            top_p=0.95,
            extra_body={"chat_template_kwargs": MODES[mode]},
            **kwargs,
        )
    except Exception as exc:
        return {"error": redact(f"{type(exc).__name__}: {str(exc)[:300]}", key)}
    message = response.choices[0].message
    extra = message.model_extra or {}
    trace = extra.get("reasoning_content") or extra.get("reasoning")
    usage = response.usage
    return {
        "ms": int((time.perf_counter() - start) * 1000),
        "prompt_tokens": usage.prompt_tokens if usage else None,
        "completion_tokens": usage.completion_tokens if usage else None,
        "finish_reason": response.choices[0].finish_reason,
        "reasoning_field": "reasoning_content"
        if extra.get("reasoning_content")
        else ("reasoning" if extra.get("reasoning") else None),
        "reasoning_field_chars": len(str(trace or "")),
        "think_tag_in_content": "<think>" in (message.content or "")
        or "</think>" in (message.content or ""),
        "content": (message.content or "")[:200],
        "tool_calls": [tc.function.model_dump() for tc in (message.tool_calls or [])],
    }


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    base = os.getenv("TOKEN_FACTORY_BASE_URL", "").strip()
    key = os.getenv("TOKEN_FACTORY_API_KEY", "").strip()
    if not base or not key:
        print("Set TOKEN_FACTORY_BASE_URL and TOKEN_FACTORY_API_KEY in .env first.")
        return 2
    client = OpenAI(base_url=base, api_key=key, timeout=180, max_retries=1)
    results: dict[str, Any] = {"base_url": base}

    # 1. Catalogue
    ids = sorted(m.id for m in client.models.list().data)
    results["all_models"] = ids
    nemotron = [i for i in ids if "nemotron" in i.lower()]
    embeds = [i for i in ids if "embed" in i.lower() or "bge" in i.lower()]
    rerankers = [i for i in ids if "rerank" in i.lower()]
    print("Nemotron:", *nemotron, sep="\n  ")
    print("Embedding:", *embeds, sep="\n  ")
    print("Rerank:", *rerankers, sep="\n  ")

    tiers = {
        "nano": os.getenv("MODEL_NANO") or pick(nemotron, "nano", "30b"),
        "super": os.getenv("MODEL_SUPER") or pick(nemotron, "super", "120b"),
        "ultra": os.getenv("MODEL_ULTRA")
        or pick(nemotron, "ultra", "550b")
        or pick(nemotron, "ultra"),
    }
    results["tiers"] = tiers
    print("\nTier mapping:", json.dumps(tiers, indent=2))

    # 2. Per model: reasoning modes, tools, schema
    for tier, model in tiers.items():
        if not model:
            print(f"\n!! {tier}: no model found")
            continue
        if model not in ids:
            print(f"\n!! {tier}: {model} is not in /v1/models")
        print(f"\n== {tier}: {model}")
        out: dict[str, Any] = results.setdefault("probe", {}).setdefault(tier, {})
        for mode in MODES:
            out[f"chat_{mode}"] = call(client, key, model, mode, messages=QUESTION)
            summary = {k: v for k, v in out[f"chat_{mode}"].items() if k != "content"}
            print(f"  chat/{mode}:", summary)
        for mode in ("off", "on"):
            out[f"tools_{mode}"] = call(
                client, key, model, mode, messages=TOOL_QUESTION, tools=TOOLS, tool_choice="auto"
            )
            print(
                f"  tools/{mode}:", out[f"tools_{mode}"].get("tool_calls") or out[f"tools_{mode}"]
            )
        out["schema_off"] = call(
            client,
            key,
            model,
            "off",
            messages=SCHEMA_QUESTION,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "app_spec", "schema": SCHEMA, "strict": True},
            },
        )
        print("  schema:", out["schema_off"].get("content") or out["schema_off"])

    # 3. Embeddings and rerank
    for model in embeds[:4]:
        try:
            emb = client.embeddings.create(
                model=model, input=["OOMKilled exit 137"], encoding_format="float"
            )
            results.setdefault("embed", {})[model] = {
                "dims": len(emb.data[0].embedding),
                "usage": emb.usage.model_dump() if emb.usage else None,
            }
            print(f"embed {model}: dim={len(emb.data[0].embedding)}")
        except Exception as exc:
            results.setdefault("embed", {})[model] = redact(str(exc)[:200], key)
    for model in rerankers[:3]:
        response = httpx.post(
            base.rstrip("/") + "/rerank",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "query": "database CPU spike after deploy",
                "documents": [
                    "RDS CPUUtilization 97%",
                    "user logged in",
                    "slow query: SELECT * FROM orders WHERE email=...",
                ],
            },
            timeout=60,
        )
        results.setdefault("rerank", {})[model] = (
            response.json() if response.status_code == 200 else redact(response.text[:200], key)
        )
        print(f"rerank {model}: {response.status_code}")

    Path("probe_results.json").write_text(json.dumps(results, indent=2))
    print("\nSaved probe_results.json. Check the Token Factory billing page for the actual spend.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
