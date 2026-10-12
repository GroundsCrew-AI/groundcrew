# Phase 4 progress (M4, M5, M6, M8 on branch `phase4-complete`)

Source of truth for scope: [docs/prompts/phase4-rizwan.md](prompts/phase4-rizwan.md). If a session
is compacted, re-read that file and this one, then continue from "Next".

## Status

| Milestone | State | Notes |
|---|---|---|
| Self-approve guard (dev only) | done | `POST /approvals/{id}/self-approve`, 404 unless dev + flag |
| M4 Builder | done | 14 tests; live: psycopg2 fixed automatically (2 runs) |
| M5 Deployer | done | 4 tests |
| M6 Pipeline + API | done | 6 end-to-end tests with fakes; preview fake deleted |
| M8 E2E + live | in progress | |

## Token Factory spend (cap $1.00 for this run)

Baseline before this run: $0.0025 (37 calls, `llm_usage`). Spend for this run is measured
against that baseline.

| When | Total in `llm_usage` | This run |
|---|---|---|
| start (2026-10-12) | $0.0025 | $0.0000 |
| after the M4 live Builder checks | $0.0161 | $0.0136 |

## Live AWS runs (at most 3; each followed by rollback and cleanup for that run)

| # | Run | Result | Cleanup |
|---|---|---|---|

## Decisions made during this run

- The M3 commit `f96ac4b` (verify waits past 0 of 0 tasks, live ECS script, cleanup `RUN=`)
  merged after PR #19 closed, so it is carried on this branch.
- `DEV_MODE_NO_ULTRA=true` set in the local `.env` (it was blank, which means false).

- Smoke failures end the run (failed, with the container's logs). Builder does not try to fix
  them: they are usually env or data problems a Dockerfile change cannot solve.
- Task size from plan_deploy is recorded but targets run "small" only.
- A deploy's id is its run id (no deploys table); the request without env values is in
  runs.state["request"].
- After a deploy is registered its resource is marked actionable in the pipeline. A later
  discovery recomputes coverage by type and can set it back (discovery code is outside the
  Phase 4 paths); noted below.

## Open issues

- Scout's detect_app_spec (Nano, low) hit its 2,000-token cap on legacy-flask-app in both live
  checks and fell back to rules only (the spec was still right). Scout and its route are outside
  the Phase 4 paths, so it is not changed here.
- Discovery recomputes `coverage` by type, so a registered deploy may go back from actionable
  to diagnosable on the next discovery until coverage also reads `config.last_deployment`.
- The GitHub connection's stored repo list has only `GroundsCrew-AI/checkout-demo`. The
  installation must be able to read `GroundsCrew-AI/legacy-flask-app` for the GitHub path.

## Next

M8: live kind and ECS runs through POST /deploys, Playwright in fake mode + CI, live script, docs.
