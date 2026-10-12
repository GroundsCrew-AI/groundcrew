# Phase 4 progress (M4, M5, M6, M8 on branch `phase4-complete`)

Source of truth for scope: [docs/prompts/phase4-rizwan.md](prompts/phase4-rizwan.md). If a session
is compacted, re-read that file and this one, then continue from "Next".

## Status

| Milestone | State | Notes |
|---|---|---|
| Self-approve guard (dev only) | in progress | needed by the live scripts |
| M4 Builder | not started | |
| M5 Deployer | not started | |
| M6 Pipeline + API | not started | |
| M8 E2E + live | not started | |

## Token Factory spend (cap $1.00 for this run)

Baseline before this run: $0.0025 (37 calls, `llm_usage`). Spend for this run is measured
against that baseline.

| When | Total in `llm_usage` | This run |
|---|---|---|
| start (2026-10-12) | $0.0025 | $0.0000 |

## Live AWS runs (at most 3; each followed by rollback and cleanup for that run)

| # | Run | Result | Cleanup |
|---|---|---|---|

## Decisions made during this run

- The M3 commit `f96ac4b` (verify waits past 0 of 0 tasks, live ECS script, cleanup `RUN=`)
  merged after PR #19 closed, so it is carried on this branch.
- `DEV_MODE_NO_ULTRA=true` set in the local `.env` (it was blank, which means false).

## Open issues

- The GitHub connection's stored repo list has only `GroundsCrew-AI/checkout-demo`. The
  installation must be able to read `GroundsCrew-AI/legacy-flask-app` for the GitHub path.

## Next

Self-approve guard, then M4.
