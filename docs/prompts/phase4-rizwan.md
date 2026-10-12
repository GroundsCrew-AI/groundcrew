# Phase 4 plan (Rizwan owns all of Phase 4)

Read this file at the start of every Phase 4 milestone. It is the source of truth for scope and decisions.
Also read PROJECT_CONTEXT.md, docs/phase4-infra.md, docs/kind.md, docs/deploy-live.md, docs/deploys-contract.md,
docs/events.md, docs/demo.md.

## Ownership
- Rizwan owns ALL of Phase 4. Ahmed works on Phase 5 in parallel (apps/api/app/agents/medic/, telemetry,
  incidents). Medic only needs Target.rollback() from Phase 4.
- Allowed paths: apps/api/app/sandbox/, app/targets/, app/agents/builder/, app/agents/deployer/, app/pipeline/,
  app/routes/deploys.py, new task routes in app/llm/router.py (add only; never change existing routes, client,
  budget, structured output or Scout), app/policy/ (minimal hooks, flag in the PR), apps/web, scripts/,
  migrations, docs, tests.
- Never edit app/agents/medic/ or telemetry/.

## Done when
GroundsCrew-AI/legacy-flask-app (never containerized) goes from GitHub to a live URL on kind AND AWS ECS without
manual edits, with at least one automatic build fix, in one live run timeline, with an approval before anything
touches infrastructure.

## Standing rules
- Every model call through TokenFactory.chat with a registered task route; spend caps apply;
  DEV_MODE_NO_ULTRA=true during development; FakeLLM in all unit tests (CI never calls Token Factory).
- No AWS write calls without Rizwan's explicit OK in the chat. Verify account 881050425883 first. us-east-2 only.
- Small commits; one draft PR per milestone based on main; stop for review after each milestone.
- No em dashes in UI or docs. Update PROJECT_CONTEXT.md progress in one block.

## Decisions already made
- Ordering: before approval, only build + smoke test run (inside the sandbox). Pushing the image (and creating the
  ECR repo) is the FIRST apply step, after approval. Preflight never writes.
- apply() and rollback() require a single-use ApprovedAction token issued only by the policy engine from an
  approved, unused approval bound to the run and plan hash. Rollback (including deleting a first deploy) is an
  approval-gated emergency action, never automatic.
- Missing env at approval time: the card offers "Set value" or "Continue without it"; this re-runs preflight only
  and re-renders the plan with the same built image (POST /deploys/{id}/env, implemented in M6).
- Transient build failures (network/registry errors, timeouts, 5xx) are retried up to 2 times with backoff,
  without counting a fix attempt or calling the model; recorded as "retrying after network error".
- Smoke test: if the app has a health path, it decides; "/" is only recorded.
- Resources are marked actionable when M6 registers a deployment, not by type.
- Milestone order: M1, M2, M7, M3, M4, M5, M6, M8.

## Milestones
- M1 Deploy API contract: DONE (PR #15).
- M2 Sandbox: DONE (PR #16).
- M7 Deploy page: DONE (PR #18); the preview fake (lib/deploys-fake.ts) is deleted in M6.
- M3 Targets (k8s, aws_ecs): PR #19. Live kind run done; live ECS run with Rizwan's OK.

### M4 Builder agent (app/agents/builder/)
- New task routes: write_dockerfile (Super, reasoning low, structured {dockerfile, rationale}); fix_build (Super,
  reasoning on, structured {dockerfile, rationale, changed_lines}); attempt 4 escalates to Ultra (Super while
  DEV_MODE_NO_ULTRA). Every enum includes "other".
- Input: Scout's AppSpec. If the repo has a Dockerfile, build it as-is first.
- Loop: build in sandbox; on failure send the failing step + log tail; patch; retry; max 5 attempts; same error
  after the same kind of fix -> stop_reason no_progress. Transient failures handled as above.
- Each attempt is a timeline step (build_attempt event) with Dockerfile diff, error summary, model badge, tokens,
  cost. Writes only inside the materialized repo copy.
- On success: smoke test. No push (push happens in apply).
- FakeLLM tests: fails twice then passes; escalation; no_progress; 5-attempt limit; transient retry; step records.
- Live check (with OK): legacy-flask-app builds after an automatic psycopg2 fix.

### M5 Deployer agent (app/agents/deployer/)
- plan_deploy route (Super, reasoning low): replicas, resources, which env vars are secret.
- Flow: plan -> target.plan render -> preflight (image built+smoked, env, port, names; missing required env blocks
  unless allow_missing_env) -> approval card (exact rendered plan, secrets masked) -> apply with token -> verify ->
  live URL. On verify failure: collect logs, offer rollback as an emergency approval.
- On success: runs.state.deploy = DeployRecord; ResourceSink.register_deployment exactly once (mark the deployed
  resource actionable).
- Secret env values: Fernet-encrypted in the job only, never in runs.state, approvals, steps or logs; purged after.

### M6 Pipeline and API
- app/pipeline/: PipelineRunner Scout -> Builder -> Deployer in one run (agent="deploy"); stage state in
  runs.state so it pauses at approvals and resumes after restarts.
- Real POST /deploys, GET /deploys, GET /deploys/{id}, POST /deploys/{id}/env replace the 501 stubs.
- Delete lib/deploys-fake.ts and the preview banner path. End-to-end tests with fakes.

### M8 E2E and live verification
- Playwright (fake mode in CI): login (dev-only test auth, never when APP_ENV != dev), Connections, Map, deploy flow.
- scripts/live_phase4.sh: deploy legacy-flask-app to kind, then ECS, via POST /deploys; Rizwan approves in the UI;
  record run id, live URL, build attempts, automatic fix yes/no, model calls, cost, duration; verify; cleanup
  (ECS cleanup APPLY=1 only with OK). Write the results table into docs/deploy-live.md as Phase 4 evidence.
- Update docs/demo.md with the final 2-minute script.
