"""Follow-up probe for Nebius Token Factory. Hard cap: $0.25 (worst case, checked before each call).

Run (you, not CI):  uv run --project apps/api python scripts/probe_followup.py

Reads TOKEN_FACTORY_BASE_URL, TOKEN_FACTORY_API_KEY, MODEL_NANO / SUPER / ULTRA / LIGHTNING,
MODEL_EMBED, and PRICE_* from the repo root .env (or the environment), like the first probe.
Unset MODEL_LIGHTNING: picked from /v1/models. Unset Lightning prices: Super's prices are assumed
for the cap (conservative). Tests:

  a. Lightning: chat off/low/on, tool call off/on, json_schema, and a 3-turn tool loop
  b. Ultra: reasoning low vs on, 5 samples each on the same prompt (average completion tokens)
  c. Latency: 5 calls each for Nano and Lightning with reasoning off (min / median / max ms)
  d. Context limits for Nano and Lightning: prompts of about 6k, 20k, and 60k tokens
  e. Embeddings with dimensions=1024: does the returned length change?
  f. Strict schema: an enum field and a prompt that pushes a value outside the enum

Prints a table and saves probe_followup.json in the current directory (git-ignored).
Never prints the API key.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

REPO_ROOT = Path(__file__).resolve().parents[1]
CAP_USD = 0.25
CHARS_PER_TOKEN = 4

MODES: dict[str, dict[str, bool]] = {
    "off": {"enable_thinking": False},
    "low": {"enable_thinking": True, "low_effort": True},
    "on": {"enable_thinking": True},
}
# USD per million tokens (in, out). Same defaults as apps/api/app/config.py (unverified).
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "nano": (0.06, 0.24),
    "super": (0.30, 0.90),
    "ultra": (1.00, 3.00),
}
EMBED_PRICE_ASSUMED = 0.10  # per million tokens, only for the cap; inputs here are tiny

QUESTION = [
    {
        "role": "user",
        "content": "A container exits with code 137 right after a deploy that changed its task "
        "definition. Most likely cause, one sentence.",
    }
]
ECS_TOOL: dict[str, Any] = {
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
LOOP_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_service_status",
            "description": "Get the status of a service by name.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restart_service",
            "description": "Restart a service by name.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
]
APP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "language": {"type": "string"},
        "port": {"type": "integer"},
        "needs_database": {"type": "boolean"},
    },
    "required": ["language", "port", "needs_database"],
    "additionalProperties": False,
}
ENUM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "language": {"type": "string", "enum": ["python", "javascript", "go", "other"]},
    },
    "required": ["language"],
    "additionalProperties": False,
}
ENUM_PROMPT = [
    {
        "role": "user",
        "content": "The repository is written entirely in Rust (Cargo.toml, src/main.rs). Set the "
        "language field to the exact name of the language, which is Rust. Do not use 'other'.",
    }
]


def redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


@dataclass
class Spend:
    cap: float
    spent: float = 0.0
    skipped: list[str] = field(default_factory=list)

    def allow(self, label: str, worst: float) -> bool:
        if self.spent + worst > self.cap:
            self.skipped.append(f"{label} (worst case ${worst:.4f})")
            return False
        return True

    def add(self, cost: float) -> None:
        self.spent += cost


@dataclass
class Probe:
    client: OpenAI
    key: str
    models: dict[str, str]
    prices: dict[str, tuple[float, float]]
    spend: Spend
    rows: list[dict[str, Any]] = field(default_factory=list)

    def cost(self, tier: str, prompt_tokens: int, completion_tokens: int) -> float:
        price_in, price_out = self.prices[tier]
        return (prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000

    def chat(
        self,
        label: str,
        tier: str,
        mode: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 1200,
        **kwargs: Any,
    ) -> dict[str, Any]:
        model = self.models.get(tier)
        row: dict[str, Any] = {"test": label, "tier": tier, "mode": mode, "model": model}
        if not model:
            row["error"] = "no model id"
            self.rows.append(row)
            return row
        prompt_estimate = len(json.dumps(messages)) // CHARS_PER_TOKEN + 50
        if not self.spend.allow(label, self.cost(tier, prompt_estimate, max_tokens)):
            row["error"] = "skipped: would pass the $ cap"
            self.rows.append(row)
            return row
        start = time.perf_counter()
        try:
            response = self.client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                max_tokens=max_tokens,
                temperature=1.0,
                top_p=0.95,
                extra_body={"chat_template_kwargs": MODES[mode]},
                **kwargs,
            )
        except Exception as exc:
            row["ms"] = int((time.perf_counter() - start) * 1000)
            row["error"] = redact(f"{type(exc).__name__}: {str(exc)[:300]}", self.key)
            # A failed call may still be billed for its prompt; count it to stay safe.
            self.spend.add(self.cost(tier, prompt_estimate, 0))
            self.rows.append(row)
            return row
        row["ms"] = int((time.perf_counter() - start) * 1000)
        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else prompt_estimate
        completion_tokens = usage.completion_tokens if usage else max_tokens
        row["cost"] = self.cost(tier, prompt_tokens, completion_tokens)
        self.spend.add(row["cost"])
        message = response.choices[0].message
        extra = message.model_extra or {}
        row.update(
            {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "finish_reason": response.choices[0].finish_reason,
                "reasoning_chars": len(
                    str(extra.get("reasoning_content") or extra.get("reasoning") or "")
                ),
                "content": (message.content or "")[:200],
                "tool_calls": [
                    {"id": tc.id, **tc.function.model_dump()}
                    for tc in (message.tool_calls or [])
                    if tc.type == "function"
                ],
            }
        )
        self.rows.append(row)
        return row


def test_lightning(p: Probe) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for mode in MODES:
        out[f"chat_{mode}"] = p.chat(f"a.lightning chat/{mode}", "lightning", mode, QUESTION)
    for mode in ("off", "on"):
        out[f"tools_{mode}"] = p.chat(
            f"a.lightning tools/{mode}",
            "lightning",
            mode,
            [{"role": "user", "content": "Check the 'worker' service in cluster 'demo'."}],
            tools=[ECS_TOOL],
            tool_choice="auto",
        )
    out["schema"] = p.chat(
        "a.lightning json_schema",
        "lightning",
        "off",
        [
            {
                "role": "user",
                "content": "Repo has package.json with next, a .env.example with DATABASE_URL, "
                "and 'next start -p 3000'. Give the app spec as JSON.",
            }
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "app_spec", "schema": APP_SCHEMA, "strict": True},
        },
    )
    out["tool_loop"] = tool_loop(p)
    return out


def tool_loop(p: Probe) -> dict[str, Any]:
    """call -> tool result -> next call -> tool result -> answer, reasoning off."""
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": "You operate services with the tools. One tool call at a time.",
        },
        {
            "role": "user",
            "content": "The 'api' service is failing. Check its status; if it is down, restart "
            "it; then tell me the result in one sentence.",
        },
    ]
    fake_results = {
        "get_service_status": "api: down (OOMKilled)",
        "restart_service": "restarted api",
    }
    turns: list[dict[str, Any]] = []
    for turn in range(1, 4):
        row = p.chat(
            f"a.lightning loop turn {turn}",
            "lightning",
            "off",
            messages,
            max_tokens=400,
            tools=LOOP_TOOLS,
            tool_choice="auto",
        )
        calls = row.get("tool_calls") or []
        turns.append(
            {
                "turn": turn,
                "tool_calls": [c["name"] for c in calls],
                "arguments_valid_json": all(_valid_json(c["arguments"]) for c in calls),
                "content": row.get("content"),
                "error": row.get("error"),
            }
        )
        if row.get("error") or not calls:
            break
        messages.append(
            {
                "role": "assistant",
                "content": row.get("content") or None,
                "tool_calls": [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {"name": c["name"], "arguments": c["arguments"]},
                    }
                    for c in calls
                ],
            }
        )
        for c in calls:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": c["id"],
                    "content": fake_results.get(c["name"], "error: unknown tool"),
                }
            )
    expected = ["get_service_status", "restart_service"]
    sequence = [name for t in turns for name in t["tool_calls"]]
    return {
        "turns": turns,
        "followed_expected_sequence": sequence == expected,
        "final_answer": turns[-1]["content"] if turns and not turns[-1]["tool_calls"] else None,
    }


def _valid_json(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except json.JSONDecodeError:
        return False


def test_ultra_low_vs_on(p: Probe) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for mode in ("low", "on"):
        rows = [p.chat(f"b.ultra {mode} #{i}", "ultra", mode, QUESTION) for i in range(1, 6)]
        tokens = [r["completion_tokens"] for r in rows if "completion_tokens" in r]
        out[mode] = {
            "samples": len(tokens),
            "avg_completion_tokens": statistics.mean(tokens) if tokens else None,
            "min": min(tokens, default=None),
            "max": max(tokens, default=None),
        }
    return out


def test_latency(p: Probe) -> dict[str, Any]:
    out: dict[str, Any] = {}
    tiny = [{"role": "user", "content": "Reply with the single word: ok"}]
    for tier in ("nano", "lightning"):
        rows = [
            p.chat(f"c.{tier} latency #{i}", tier, "off", tiny, max_tokens=16) for i in range(5)
        ]
        ms = [r["ms"] for r in rows if "ms" in r and "error" not in r]
        out[tier] = {
            "calls": len(ms),
            "min_ms": min(ms, default=None),
            "median_ms": statistics.median(ms) if ms else None,
            "max_ms": max(ms, default=None),
        }
    return out


def _long_prompt(tokens: int) -> list[dict[str, Any]]:
    line = "the quick brown fox jumps over the lazy dog beside the quiet river bank."
    count = max(1, tokens * CHARS_PER_TOKEN // (len(line) + 12))
    body = "\n".join(f"Line {i:05d}: {line}" for i in range(1, count + 1))
    question = "What is the number of the last line above? Reply with the number only."
    return [{"role": "user", "content": f"{body}\n\n{question}"}]


def test_context(p: Probe) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for tier in ("nano", "lightning"):
        for size in (6_000, 20_000, 60_000):
            row = p.chat(
                f"d.{tier} context ~{size // 1000}k", tier, "off", _long_prompt(size), max_tokens=16
            )
            out[f"{tier}_{size}"] = {
                "ok": "error" not in row,
                "prompt_tokens": row.get("prompt_tokens"),
                "answer": row.get("content"),
                "error": row.get("error"),
            }
    return out


def test_embedding(p: Probe, model: str) -> dict[str, Any]:
    if not p.spend.allow("e.embedding dimensions=1024", 50 * EMBED_PRICE_ASSUMED / 1_000_000):
        return {"error": "skipped: would pass the $ cap"}
    try:
        response = p.client.embeddings.create(
            model=model, input=["OOMKilled exit 137"], encoding_format="float", dimensions=1024
        )
    except Exception as exc:
        return {"model": model, "error": redact(f"{type(exc).__name__}: {str(exc)[:300]}", p.key)}
    tokens = response.usage.prompt_tokens if response.usage else 50
    p.spend.add(tokens * EMBED_PRICE_ASSUMED / 1_000_000)
    return {
        "model": model,
        "requested_dimensions": 1024,
        "returned_length": len(response.data[0].embedding),
        "dimensions_honoured": len(response.data[0].embedding) == 1024,
    }


def test_strict_enum(p: Probe) -> dict[str, Any]:
    out: dict[str, Any] = {}
    allowed = ENUM_SCHEMA["properties"]["language"]["enum"]
    for tier in ("nano", "super", "ultra", "lightning"):
        row = p.chat(
            f"f.{tier} strict enum",
            tier,
            "off",
            ENUM_PROMPT,
            max_tokens=200,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "lang", "schema": ENUM_SCHEMA, "strict": True},
            },
        )
        value: Any = None
        try:
            value = json.loads(row.get("content") or "").get("language")
        except (json.JSONDecodeError, AttributeError):
            value = None
        out[tier] = {
            "raw": row.get("content"),
            "language": value,
            "inside_enum": value in allowed,
            "error": row.get("error"),
        }
    return out


def print_table(rows: list[dict[str, Any]]) -> None:
    headers = ("test", "model", "mode", "ok", "prompt", "compl", "ms", "cost $", "note")
    table = [
        (
            r["test"],
            (r.get("model") or "-").split("/")[-1][:28],
            r["mode"],
            "no" if r.get("error") else "yes",
            str(r.get("prompt_tokens", "-")),
            str(r.get("completion_tokens", "-")),
            str(r.get("ms", "-")),
            f"{r['cost']:.5f}" if "cost" in r else "-",
            (r.get("error") or ",".join(c["name"] for c in r.get("tool_calls") or []))[:50],
        )
        for r in rows
    ]
    widths = [max(len(h), *(len(row[i]) for row in table)) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)))
    print("-" * (sum(widths) + 2 * len(widths)))
    for row in table:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)))


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    base = os.getenv("TOKEN_FACTORY_BASE_URL", "").strip()
    key = os.getenv("TOKEN_FACTORY_API_KEY", "").strip()
    if not base or not key:
        print("Set TOKEN_FACTORY_BASE_URL and TOKEN_FACTORY_API_KEY in .env first.")
        return 2
    client = OpenAI(base_url=base, api_key=key, timeout=180, max_retries=0)

    ids = sorted(m.id for m in client.models.list().data)
    lightning = os.getenv("MODEL_LIGHTNING") or next(
        (i for i in ids if "lightning" in i.lower()), ""
    )
    models = {
        "nano": os.getenv("MODEL_NANO", ""),
        "super": os.getenv("MODEL_SUPER", ""),
        "ultra": os.getenv("MODEL_ULTRA", ""),
        "lightning": lightning,
    }
    prices = {
        tier: (
            float(os.getenv(f"PRICE_{tier.upper()}_INPUT_PER_M") or default[0]),
            float(os.getenv(f"PRICE_{tier.upper()}_OUTPUT_PER_M") or default[1]),
        )
        for tier, default in DEFAULT_PRICES.items()
    }
    lightning_in = os.getenv("PRICE_LIGHTNING_INPUT_PER_M")
    lightning_out = os.getenv("PRICE_LIGHTNING_OUTPUT_PER_M")
    prices["lightning"] = (
        float(lightning_in) if lightning_in else prices["super"][0],
        float(lightning_out) if lightning_out else prices["super"][1],
    )
    print("Models:", json.dumps(models, indent=2))
    if not (lightning_in and lightning_out):
        print("Lightning prices unset: assuming Super's prices for the cap.")
    print(f"Hard cap: ${CAP_USD:.2f} (worst case per call, checked before calling)\n")

    probe = Probe(client, key, models, prices, Spend(CAP_USD))
    results: dict[str, Any] = {"base_url": base, "models": models, "prices_used": prices}
    results["a_lightning"] = test_lightning(probe)
    results["b_ultra_low_vs_on"] = test_ultra_low_vs_on(probe)
    results["c_latency"] = test_latency(probe)
    results["d_context"] = test_context(probe)
    results["e_embedding"] = test_embedding(
        probe, os.getenv("MODEL_EMBED") or "Qwen/Qwen3-Embedding-8B"
    )
    results["f_strict_enum"] = test_strict_enum(probe)
    results["calls"] = probe.rows
    results["estimated_spend_usd"] = round(probe.spend.spent, 5)
    results["skipped_for_cap"] = probe.spend.skipped

    print_table(probe.rows)
    print("\nSummary")
    print("  a. Lightning tool loop:", json.dumps(results["a_lightning"]["tool_loop"]["turns"]))
    print("  b. Ultra low vs on:", json.dumps(results["b_ultra_low_vs_on"]))
    print("  c. Latency:", json.dumps(results["c_latency"]))
    ctx = {k: v["ok"] for k, v in results["d_context"].items()}
    print("  d. Context ok:", json.dumps(ctx))
    print("  e. Embedding:", json.dumps(results["e_embedding"]))
    enum = {k: (v["language"], v["inside_enum"]) for k, v in results["f_strict_enum"].items()}
    print("  f. Strict enum (value, inside enum):", json.dumps(enum))
    print(f"\nEstimated spend: ${probe.spend.spent:.4f} of the ${CAP_USD:.2f} cap.")
    if probe.spend.skipped:
        print("Skipped to stay under the cap:", *probe.spend.skipped, sep="\n  ")

    Path("probe_followup.json").write_text(json.dumps(results, indent=2, default=str))
    print("Saved probe_followup.json. Check the Token Factory billing page for the actual spend.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
