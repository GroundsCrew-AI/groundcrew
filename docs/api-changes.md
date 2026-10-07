# API changes for the web app (Phase 4)

For the frontend lead. Phase 4 adds data the web app does not show yet. Each entry says what changed, where
it appears, and what to do with it. The generated client (`apps/web/lib/api/`) is regenerated with
`make api-types`; no hand-written web code was changed.

New fields are marked **optional** in the OpenAPI schema for now, so the existing web fixtures still compile.
The API always sends them. When the web app starts using them, remove the field names from the
`_optional_in_schema(...)` calls in `apps/api/app/routes/schemas.py` and run `make api-types`, so they become
required in TypeScript.

## Step 1: LLM layer (model, reasoning, and cost on every step)

Judges score how well we use Token Factory and Nemotron, so every model call should show which model ran,
how much it thought, and what it cost.

### `StepOut` (in `RunDetail.steps` and the SSE `step`, `approval_requested`, `approval_decided` events)

| Field | Type | Meaning | Suggested UI |
|---|---|---|---|
| `model_tier` (existing) | `"nano" \| "super" \| "ultra" \| null` | Nemotron 3 tier of an `llm` step | Model badge |
| `model_id` (existing) | `string \| null` | Exact Token Factory model ID | Tooltip on the badge |
| `reasoning_mode` | `"off" \| "low" \| "on" \| null` | How much the model was allowed to think | Small chip next to the badge |
| `cost_usd` (existing) | `number \| null` | Cost of this call in USD | Show to 4 decimals (calls cost fractions of a cent) |
| `ultra_equiv_usd` | `number \| null` | What the same tokens would have cost on Ultra | "saved x%" = `1 - cost_usd / ultra_equiv_usd` |
| `fell_back` | `boolean` | The route's first model failed and its fallback tier answered | Note: "fell back to Super" |
| `escalated` | `boolean` | Called one tier up with full reasoning after cheaper attempts failed | Note: "escalated" (Builder's 4th build attempt) |
| `reasoning_excerpt` | `string \| null` | Start of the model's reasoning trace, redacted, at most 2,000 characters | Collapsed "Show reasoning" section |

`output_summary` of a schema repair call starts with `(schema repair) `: the model's first JSON was invalid
and it was asked once to fix it. Both calls are separate steps.

### `RunOut` / `RunDetail` and the SSE `status` / `done` events (`RunStatusEvent`)

| Field | Type | Meaning |
|---|---|---|
| `cost_usd` (existing) | `number \| null` | Total cost of the run |
| `ultra_equiv_usd` | `number \| null` | What the run would have cost on Ultra only. Show the saving beside the cost. |

### `WorkspaceSummary` (`GET /workspaces/{id}/summary`)

Token Factory spend across all workspaces. Counted in the new `llm_usage` table.

| Field | Type | Meaning |
|---|---|---|
| `usd_today` | `number` | Spent today (UTC) |
| `daily_usd_cap` | `number \| null` | `LLM_DAILY_USD_CAP` (2.00 in dev, 0.50 on the hosted demo) |
| `usd_total` | `number` | Spent in total |
| `total_usd_cap` | `number \| null` | `LLM_TOTAL_USD_CAP` (25): the hard stop |
| `budget_tokens_today` (existing) | `number` | Now read from `llm_usage` |

### Budget stops

A run that hits a cap still ends with `status: "stopped"` and `stop_reason: "budget"`. The exact cap is now
the start of `error`, for example `run_usd_cap: run spent $0.40 (LLM_RUN_USD_CAP=0.4)`. The caps are
`run_usd_cap`, `run_token_cap`, `ultra_call_cap`, `incident_ultra_cap`, `daily_usd_cap`, `daily_token_cap`,
and `total_usd_cap`. The last step of the run has `kind: "budget"` with the same text.

## Coming in later steps

- Step 5: `stop_reason: "no_progress"` (Builder repeated the same failing fix). `components/runs/badges.tsx`
  keys a `Record<StopReason, string>`, so it needs a label when this lands.
- Step 8: `POST /deploys`, `GET /deploys/{id}`, and the Scout / Builder / Deployer events, documented in
  `docs/events.md`.
