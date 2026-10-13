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

## Deploy contract (Milestone 1)

The full contract is in `docs/deploys-contract.md`; the events are in `docs/events.md`.

- `POST /deploys`, `GET /deploys`, `GET /deploys/{deploy_id}`: typed in OpenAPI (`DeployCreate`,
  `DeployCreated`, `DeployPage`, `DeployDetail`), returning 501 until Milestone 6.
- `StopReason` gains `no_progress` (Builder repeated the same fix for the same error).
  `components/runs/badges.tsx` already had the label; its temporary `KnownStopReason` type is gone.
- `StepOut.data` (optional in the schema): the typed payload of a deploy step, discriminated by
  `type`. Null on every other step.
- New SSE event names for deploy runs: `stage_started`, `stage_finished`, `build_attempt`,
  `verify_result`, `live_url`, `rollback_offered`, `run_finished`. Each sends a `StepOut`.
  `approval_requested` for a deploy carries the plan in `step.data` (`type: "deploy_plan"`).

## Phase 5 M0: incidents contract

For the Incidents UI (Rizwan). Contract: `docs/phase5-interfaces.md`.

- New routes `POST /incidents`, `GET /incidents` (filters `status`, `severity`, `resource`,
  `since`; cursor paging), `GET /incidents/{incident_id}`: typed, answer 501 until Phase 5 M4.
- `StepOut.data` is now `DeployEventData | MedicEventData`: incident steps carry `IncidentOpened`,
  `IncidentUpdated`, `EvidenceCollected`, `MemoryChecked`, `ResearchDone`, `Diagnosis`, `FixProposed`, `FixApproval`,
  `FixApplied`, `IncidentResolved`, or `IncidentFailed`, discriminated by `type`. Existing deploy
  code that narrows on `data.type` is unaffected (web typecheck and tests pass unchanged).
- `SseEventName` adds `incident_opened`, `incident_updated`, `evidence_collected`, `memory_checked`, `research_done`,
  `diagnosis`, `fix_proposed`, `fix_applied`, `incident_resolved`, `incident_failed`.

## Phase 5 M4: incidents API live

- `GET /incidents` (filters `status`, `severity`, `resource`, `since`; `limit`, `cursor`) and
  `GET /incidents/{incident_id}` now answer with the M0 shapes (`IncidentPage`, `IncidentDetail`);
  the 501 responses are gone from the schema. No field changed.
- `POST /runs/{run_id}/stop` on a Medic run also closes its incident (`incident_updated` and
  `incident_failed` steps follow `stopped` on the stream).
- `fixes[].pull_request.state` and `pull_requests[].state` move from `open` to `merged` or
  `closed` as GitHub reports them.

## Phase 5: Operate tier (emergency fixes)

- `POST /connections/{connection_id}/aws/operate` with `{"enabled": true | false}`: turns approved
  emergency fixes on (verifies the `groundcrew-operate` role) or off.
- `ConnectionOut` adds `capabilities` (`observe`, `deploy`, `operate`; null for non-AWS) and
  `operate_role_arn`. Suggested UI: an "Emergency fixes" toggle on the AWS card, next to deploys.

## Phase 5: broken-env fixes (2026-10-13)

All additive.
- `ProposedFix.depends_on` (fix ids): a restart depends on the secret rollback.
  `POST /approvals/{id}/decide` with `approve` answers 409 while a prerequisite is not applied
  ("Fix 1 (...) must be applied before this one."); `reject` is always allowed. If a
  prerequisite is rejected or fails, the dependent fix is closed without running: its approval
  becomes `rejected` with `decided_via` "Medic: fix 1 was rejected", and a `fix_applied` step
  with `ok: false` says why.
- `IncidentOpened.merged_from` and `IncidentUpdated.merged_into`: an incident on related
  resources that failed in the last 30 minutes (not stopped by a person) is taken over by the
  next one: its signals and resources join the new incident, and its run gets an
  `incident_updated` step (status stays `failed`) pointing at the new one.
- `rds_cpu_latency` also fires on slow queries (request p50 + database CPU); the name is
  unchanged.

## Phase 5: missing-index run fixes (2026-10-13)

All additive.
- `ChangeMarker.commit` (full SHA; a deploy's head commit) and `ChangeMarker.repo`
  (`owner/name`) on GitHub commit and deploy markers.
- `RootCause.search_query`: the model's web search for the root cause's mechanism. Medic runs
  it after the diagnosis as a second `research_done` step (within
  `TAVILY_MAX_CALLS_PER_INCIDENT`), so a run can have two `research_done` steps.
- `time_to_detect_ms` is timed from the signal that opened the incident (or the triggering
  change, if later), not from a merged incident's earlier signals.

## Phase 5: live updates, false alarms, verify rounds (2026-10-13)

All additive.
- `VerifyResult.waiting` (string or null) and `VerifyResult.check` (round number; null for a
  deploy's single verify). Medic rounds before the verify deadline are not failures: show
  "Waiting for <waiting>" or "Not recovered yet" with "check n"; the incident fails only with
  `incident_failed`. The timeline shows consecutive rounds that say the same thing as one row.
- `RootCause.false_alarm`: Medic's diagnosis says nothing is wrong for users. It must pass a
  live check (errors, health, latency, database) first; if it passes, the incident resolves with
  "False alarm: ..." and no fixes.
- `PullRequestRef.merged_at`: set when GitHub reports the fix PR merged.
- New signals on an incident whose fixes wait for approval (a new rule, or an error spike 3x the
  largest before) close the pending approvals (`decided_via` "Medic: superseded by new signals")
  and Medic investigates again; an `incident_updated` step says why. Fixes from a re-diagnosis
  have ids like `fix-1-r1`.
