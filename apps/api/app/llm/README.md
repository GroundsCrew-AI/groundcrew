# llm

The only code that talks to Nebius Token Factory. Every model call is `TokenFactory.chat(task, ...)`.

- `models.py`: tiers (Nano / Super / Ultra), reasoning modes (off / low / on), prices and context sizes from settings.
- `router.py`: task to tier + reasoning mode + token cap + fallback tier; `escalate=True` bumps one tier.
- `client.py`: `TokenFactory` with retries, fallback, JSON-schema output with one repair, reasoning capture
  (shown, never re-sent), cost and Ultra-equivalent cost, the `on_call` hook, `embed()`, `rerank()`.
- `budget.py`: `RunBudget` (per-run USD, token, and Ultra caps) and `PgUsageStore` (`llm_usage` table: daily and
  total USD caps).
- `recorder.py`: the `on_call` hook for runs: writes the `run_steps` row and the run totals.
- `structured.py`: strict JSON schema, schema-in-prompt, tolerant JSON parsing.
- `tool_calls.py`: native and text tool-call parsing, reasoning split.
- `fake.py`: `FakeLLM`, the real client with scripted replies instead of HTTP. Tests use it; it never calls the API.
- `evidence.py`: Medic's evidence funnel (Phase 5, not wired yet).
- `costs.py`: cost helpers for scripts and reports.
