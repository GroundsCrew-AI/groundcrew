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
| M8 E2E + live | done | Playwright 4 passed; live kind, ECS run 1, and the human-approved kind run `f196e111` |

## Token Factory spend (cap $1.00 for this run)

Baseline before this run: $0.0025 (37 calls, `llm_usage`). Spend for this run is measured
against that baseline.

| When | Total in `llm_usage` | This run |
|---|---|---|
| start (2026-10-12) | $0.0025 | $0.0000 |
| after the M4 live Builder checks | $0.0161 | $0.0136 |
| after live kind run 1 and ECS run 1 | $0.0227 | $0.0202 |

## Live AWS runs (at most 3; each followed by rollback and cleanup for that run)

| # | Run | Result | Cleanup |
|---|---|---|---|
| 1 | `a75b5513-48f1-4cfd-bebd-e2c22e183626` | live at the task IP, /health 200, on the Map; rolled back (service deleted, emergency approval) | `cleanup-deploys RUN=` deleted 4 of 4 (SG, log group, ECR repo, secret); task definition `:2` kept by design |

## Made for Phase 5 (Ahmed)

Done on this branch, on top of main with PR #22 (both sets of approved actions kept in
`app/policy/actions.py`):

- **Resume handlers by agent**: `app.pipeline.resume.register_resume_handler(agent, handler)`.
  The worker's "resume_run" picks the handler for `run.agent` ("deploy" is the pipeline) and
  falls back to AgentRunner. Medic can now `register_resume_handler("medic", ...)`.
- **VerifyCheck names**: `"errors"` and `"metric"` are valid `VerifyCheck.name` values (additive;
  API types regenerated). Medic can switch from `"other"`.

## Decisions made during this run

- The M3 commit `f96ac4b` (verify waits past 0 of 0 tasks, live ECS script, cleanup `RUN=`)
  merged after PR #19 closed, so it is carried on this branch.
- `DEV_MODE_NO_ULTRA=true` set in the local `.env` (it was blank, which means false).
  `LIVE_SCRIPT_SELF_APPROVE=1` added to the local `.env` for the live script (dev only).
- PR #21 was merged mid-run (through the M6 fixtures fix); the rest continues on the same
  branch, rebased on main, in a follow-up PR.
- The ECS secret step is labelled `secretsmanager/Secret`: display redaction turned
  `secretsmanager:Secret` into `[REDACTED]` on the approval card (seen in ECS run 1).

- Smoke failures end the run (failed, with the container's logs). Builder does not try to fix
  them: they are usually env or data problems a Dockerfile change cannot solve.
- Task size from plan_deploy is recorded but targets run "small" only.
- A deploy's id is its run id (no deploys table); the request without env values is in
  runs.state["request"].
- After a deploy is registered its resource is marked actionable in the pipeline. A later
  discovery recomputes coverage by type and can set it back (discovery code is outside the
  Phase 4 paths); noted below.

## Open issues

- A build that times out (SANDBOX_BUILD_TIMEOUT_SECONDS, 900 s) counts as a network failure and
  is retried twice, so one slow attempt can take up to 45 minutes. Consider retrying a timeout
  once, or with a shorter limit.

- The first human-approved run (2026-10-12) failed at Plan: the stack had been recreated
  without the kind override, so the worker could not resolve `groundcrew-control-plane`. The
  stack is back on `make dev KIND=1`. Fixed in code: the error now says how to fix it, and the
  stage that was running ends as failed instead of showing "running".
- LLM step summaries are redacted for display, which shows `"secret_env": [REDACTED]` for the
  plan_deploy answer (the redactor reads `secret_env": [` as a secret). Display only; the
  redaction rules are shared security code and were not loosened.

- Scout's detect_app_spec (Nano, low) hit its 2,000-token cap on legacy-flask-app in both live
  checks and fell back to rules only (the spec was still right). Scout and its route are outside
  the Phase 4 paths, so it is not changed here.
- Discovery recomputes `coverage` by type, so a registered deploy may go back from actionable
  to diagnosable on the next discovery until coverage also reads `config.last_deployment`.
- The GitHub connection's stored repo list has only `GroundsCrew-AI/checkout-demo`. The
  installation must be able to read `GroundsCrew-AI/legacy-flask-app` for the GitHub path.

## Next

M8: live kind and ECS runs through POST /deploys, Playwright in fake mode + CI, live script, docs.
