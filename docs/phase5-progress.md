# Phase 5 progress (Medic)

Owner: Ahmed (all of Phase 5 since 2026-10-12, including the parts planned for Rizwan). Plan: `docs/prompts/phase5-ahmed.md`. Contract: `docs/phase5-interfaces.md`.

## Status

| Milestone | Branch | PR | State |
|---|---|---|---|
| M0 Interfaces | `phase5-m0-interfaces` | #22, #23 | Merged 2026-10-12 |
| M1 Telemetry readers | `phase5-m1-telemetry` | #25 | Merged 2026-10-12 |
| M2 Detection | `phase5-m2-detection` | #26 | Merged 2026-10-12 |
| M3 Medic agent | `phase5-m3-medic` | #27 | Merged 2026-10-12 |
| M4 Incidents API + wiring | `phase5-m4-incidents-api` | #28 | Merged 2026-10-12 |
| Operate tier (was Rizwan's) | `phase5-operate` | #29 | Merged 2026-10-13 |
| Incidents UI (was Rizwan's) | `phase5-incidents-ui` | #30 | Merged 2026-10-13 |
| Emergency fixes toggle | `phase5-operate-toggle` | draft | Done, in review |
| M5 Live verification | | | Needs AWS CLI, Terraform, the groundcrew-demo profile, Tavily key, and an OK for live runs |

## M0 (2026-10-12)

Done:
- `docs/phase5-interfaces.md`: evidence, emergency actions (plan + execute), fix PRs, approvals,
  RootCause, incidents API, incident run events, per-scenario expectations, questions for Rizwan.
- Types and 501 routes: `app/agents/medic/{events,schemas}.py`, `app/routes/incident{s,_schemas}.py`.
- Fakes with real approval checks: `FakeEmergencyActions` (`app/actions/fake.py`),
  `FakeFixPullRequests` (`app/tools/github_pr.py`), `FakeEvidence` (`app/telemetry/evidence.py`).
- `ApprovedAction` accepts Medic's action names; incident events stream by name; web types
  regenerated.
- Tests: `tests/test_phase5_contract.py` (19).
- Rizwan's answers folded in (2026-10-12): scoped Operate capability (`EnableOperate`,
  `OperateServiceArns` / `OperateSecretArns`, target Oct 17); evidence items with stable ids and
  claims citing them; change markers; plan lines; PR state; outcome; memory reuse;
  `incident_updated`; list filters `status`, `severity`, `resource`, `since`.

Decisions:
- An incident is investigated by a run (`agent="medic"`, `runs.incident_id`); its events are run
  steps with typed `data`, like deploys. The UI reuses the run stream, approvals, and Stop.
- Emergency actions are plan (read-only) then execute (approved), with a state re-check, like
  `Target.rollback_plan()` / `rollback()`.
- Recovery checks reuse the deploy `VerifyResult` payload.
- The model cites evidence by stable id (`ev-` + hash), not by copying lines; ids not in the
  collected evidence are removed and uncited claims dropped.
- Fix PRs get their own write allowlist in `app/tools/github_pr.py`; `app/connectors/github.py`
  stays GET only.

Open issues:
- Waiting on Rizwan: Operate role and `app/actions/` (target Oct 17), `resume_run` dispatch by
  agent and `VerifyCheck` `errors` / `metric` (his Phase 4 M6). Medic uses fakes and sends
  `other` for those checks until then.
- `tests/test_sandbox.py::test_build_and_smoke_on_a_real_daemon` fails locally (no buildx in the
  local Docker); unrelated, passes where the daemon has buildx.

Spend: $0 (no model, Tavily, AWS, or GitHub calls).

## M1 (2026-10-12)

Done:
- Readers in `app/telemetry/`: CloudWatch Logs (errors and tracebacks with one line of context,
  per-minute latency summaries per path, a sample of the rest), ECS (service events, deployments
  and rollout state, stopped tasks with exit codes, OOM marked), RDS (CPU, connections, read
  latency, read IOPS; spikes against the window's baseline), Secrets Manager (DescribeSecret
  metadata: last changed, version stages), GitHub (commits with changed files, workflow runs),
  Vercel (production deployments). Each also returns change markers (deploys, commits, secret
  changes) for the timeline chart.
- `collect()` / `GraphEvidenceCollector`: the affected resources plus neighbors over `uses`,
  `logs_to`, `reads`, `deployed_by`, `defined_in` and `calls` (both ways); readers run in
  parallel with a 45 s timeout; a failed reader leaves one line and the rest still count.
- Dev-only `GET /workspaces/{id}/evidence?resource=&since=&until=` (404 unless APP_ENV=dev).
- `make live-evidence WORKSPACE=<id> RESOURCES="..."`: read-only live check, refuses AWS
  connections outside 881050425883. Not run yet (needs the demo stack up).
- Tests: `tests/test_telemetry.py` (15) on fixtures shaped like bad-deploy-oom, missing-index
  and broken-env, with real boto3 clients and the read-only guard.

Decisions:
- No template change: every AWS call is already in the read-only allowlist and role.
  `DescribeSecret` returns version ids and stages, so `ListSecretVersionIds` is not needed.
- Request logs with a latency field become one summary line per minute and path, so
  missing-index shows as `p50` climbing on `/orders/search` instead of thousands of lines.
- A repo with no commits or runs in the window says so in one line: for broken-env, "no code
  change" is part of the evidence.
- Commits and workflow runs are read from 30 minutes before the window (a deploy takes minutes).
- `until` is clamped to now and never before `since`.

Open issues:
- ECS keeps stopped tasks for about an hour; an incident investigated later sees only service
  events for older stops.
- `make live-evidence` needs the checkout-demo stack up (Rizwan's rebuild).

Spend: $0.

## M2 (2026-10-12)

Done:
- Migration `9c6e4a1f3d52` (incidents): `detected_by`, `detector_rule`, `window_start` /
  `window_end`, `first_signal_at`, `affected_resource_ids`, `group_resource_ids`, `signals`,
  `state`; index on (workspace, status).
- `app/incidents/store.py`: `open_incident` (per-workspace advisory lock; joins the oldest open
  incident whose group overlaps, appending signals and resources; otherwise opens the incident,
  its Medic run with an `incident_opened` step, and an `investigate_incident` job) and
  `set_status` (records `incident_updated`; resolved/failed set `window_end`).
- `app/incidents/rules.py`: health_check, ecs_deployment_failed, ecs_tasks_stopped (exit 137 /
  OOM counted), log_error_spike (5 min vs the 25 before), rds_cpu_latency (CPU and read latency
  together). Pure functions on API responses plus read-only fetchers.
- `app/incidents/watcher.py`: one pass over linked ECS services (and their `logs_to` log
  groups) and RDS databases; failed checks are logged and skipped; signals older than the last
  closed incident on that resource are dropped; signaled resources whose groups overlap open one
  incident. Self-rescheduling `watch_incidents` job; at most one queued; the worker starts the
  chain when `INCIDENT_WATCH_ENABLED=true`.
- `POST /incidents` works (202, `deduped`, 404 for a resource outside the workspace).
- Interim `investigate_incident` handler: marks the incident investigating and ends the run;
  M3 replaces it with Medic.
- Tests: `tests/test_incidents_detection.py` (15).

Decisions:
- Resource group = affected resources plus one-hop graph neighbors in either direction. A new
  report joins an open incident when the two groups overlap. The api and the worker share the
  database, so an RDS spike and a worker crash become one incident, whichever comes first
  (CI caught the first version comparing only the new report's resources, which depended on
  order).
- The first signal (earliest) names the incident's detector rule and title; severity is the
  highest of its signals (health_check critical; deploy failed and crashes high; logs and RDS
  medium; reports high unless the person says otherwise).
- A watcher incident's window starts 30 minutes before its first signal (the deploy or secret
  change usually comes first); a report's starts `INCIDENT_DEFAULT_WINDOW_MINUTES` before it.
- Watcher off by default (`INCIDENT_WATCH_ENABLED`), so tests and fresh installs make no AWS calls.

Open issues:
- Thresholds (5 errors and 3x, CPU 40 % and 2x, read latency 2 ms and 2x) are guesses until the
  live missing-index run (M5); the search load may not push a t4g.micro past them.
- Detection does not use an HTTP health check: the demo api has no stable endpoint (no load
  balancer; the task IP changes), so ECS container health stands in for it.

Spend: $0.

## M3 (2026-10-12)

Done:
- `app/agents/medic/agent.py` (`MedicRunner`), jobs in `jobs.py`:
  - `investigate_incident`: gather (`collect()`, then `evidence.rank`: heuristic score, plus
    embedding cosine when embeddings are priced; every source keeps its best lines; top 40 with
    stable ids) -> memory (`memory.similar`: pgvector cosine, or a rules + resources fallback
    without embeddings) -> research (Tavily, one query from the strongest error line, capped per
    incident, skipped on a memory hit) -> diagnose (`root_cause`, or `confirm_root_cause` on
    Super when a past incident is reused; `diagnose.check` keeps only cited claims) -> propose.
    Each stage records its event and saves `incidents.state` (`state.py`). Hard timeout.
  - Fixes (`fixes.py`): emergency actions by rule when production is down
    (secret change: `secret_rollback_version` then `force_redeploy` of every service that uses
    the secret; commit/deploy: `ecs_rollback_service` of the crashing services); a fix PR from
    `propose_fix` when a commit started it, guarded by `check_draft`, shown as diffs.
  - One approval per fix; `resume` (registered for agent "medic" in Rizwan's resume registry)
    applies approved fixes in order with single-use tokens and waits at the first undecided one
    (run row locked before the last look, so a decision cannot slip between).
  - `verify_incident` job: re-checks the signals (rollout, health, errors, metric) every
    `MEDIC_VERIFY_INTERVAL_SECONDS`; two healthy rounds resolve and remember; the deadline fails
    with what still fails.
- `app/tools/github_pr.py`: `GitHubFixPullRequests` (branch, contents API commits, PR; its own
  write allowlist and body checks) and the reads for PR context; `app/tools/tavily.py`.
- New route `confirm_root_cause` (Super, low, 4,000 tokens). New settings in `.env.example`.
- The M2 placeholder handler is gone; `investigate_incident` and `verify_incident` run Medic.
- Tests: `tests/test_medic.py` (11): each scenario from incident to fixes on FakeLLM; approvals out
  of order; verify to resolved; the memory hit on a re-run (no research, Super); every fix
  rejected; verify timeout; Token Factory missing; post-check; PR guardrails; the GitHub writes.

Decisions:
- Emergency actions come from rules over the cited root cause and the graph, never from model
  text; the model writes only PR file contents, which a person reviews as a diff.
- Fixes apply strictly in order (an emergency action before its PR); verification starts once
  every fix is decided.
- A memory hit reuses the past diagnosis and fix as context, skips research, and diagnoses on
  Super: cheaper and faster, still checked against this incident's evidence.
- Without the Operate role (`app.actions.get_emergency_actions` not there yet) Medic offers no
  emergency action and says so; nothing pretends to roll back.

Open issues:
- Stopping a waiting Medic run (POST /runs/{id}/stop) leaves the incident `waiting_approval`;
  M4 should close the incident when its run is stopped.
- A fix PR's verify waits for a person to merge it and the pipeline to deploy; the default
  verify window (20 minutes) may be short for that.

Spend: $0 (FakeLLM only).

## M4 (2026-10-12)

Done:
- `GET /incidents` (status, severity, resource, since; cursor paging, newest first) and
  `GET /incidents/{id}` built from the incident row, Medic's `incidents.state`, and every run of
  the incident (`app/incidents/views.py`): tokens and cost summed across runs, evidence items,
  change markers, cited root cause, fixes with approval ids, PR links and state, verify result,
  outcome, memory reuse, research. The PR draft's full file contents stay internal.
- Fix PR state refreshed from GitHub (GET pulls) on detail reads, at most once a minute.
- Stopping a Medic run closes its incident as failed ("Stopped by <login>."), with
  `incident_updated` and `incident_failed` steps; pending fixes become rejected.
- `MEDIC_PR_VERIFY_TIMEOUT_SECONDS` (default 3600) when only a fix PR was applied (a person
  merges, the pipeline deploys, then the signals can recover).
- Wiring: Medic's resume handler is registered with Rizwan's registry (since M3);
  `investigate_incident` and `verify_incident` are worker handlers; the approvals inbox and run
  stream show Medic runs with no change.
- Web types regenerated (only the 501 responses removed). Tests: `tests/test_incidents_api.py` (4).

Open issues:
- Emergency actions need Rizwan's `app.actions.get_emergency_actions` (Operate role, target
  Oct 17); until then incidents get the fix PR and a note.
- `VerifyCheck` `errors` / `metric` are used directly (they landed in #24).
- M5 needs the checkout-demo stack rebuilt, the Operate role, and Rizwan's OK for live runs.

Spend: $0.

## Operate tier (2026-10-12)

Ahmed took over Rizwan's Phase 5 parts. Done:
- `infra/aws-connect.yaml`: `EnableOperate` (independent of `Tier`), `OperateServiceArns`,
  `OperateSecretArns`, a Rule refusing operate without real ARNs, the `groundcrew-operate` role
  (UpdateService and DescribeServices on the listed services; ListTaskDefinitions and
  DescribeTaskDefinition on `*`; UpdateSecretVersionStage and DescribeSecret on the listed
  secrets; the same explicit Deny on secret values), output `OperateRoleArn`. `cfn-lint` clean.
  Observe and deploy render exactly as before (snapshot tests). API copy synced (test).
- `AWS_OPERATE_OPERATIONS`, `aws_operate_session()` (both providers), `capabilities(conn)`,
  `operate_role_arn()`, `POST /connections/{id}/aws/operate`, `ConnectionOut.capabilities`.
- `app/actions/aws.py`: the three actions as plan (read) and execute (token first, state
  re-check, then one write); `app.actions.get_emergency_actions` for Medic.
- `make plan-emergency`: read-only dry run of the plans against a live connection.
- Tests: template (5 new), sessions (3), route (1), actions on real boto3 clients with the
  operate allowlist (5). Real botocore validation caught a short secret version id in a fixture.

Not run live yet: the stack update with EnableOperate=true and `make plan-emergency` (M5).

Spend: $0.

## Incidents UI (2026-10-12)

Done (`apps/web`):
- `/incidents`: list with status tabs (all, needs approval, resolved, not resolved), severity,
  affected resources, detector, time to resolve, cost; paging; **Report incident** dialog
  (resources from the Map graph, what you see, severity) that opens the incident page.
- `/incidents/[id]`: status, severity, detector, opened, time to detect and resolve, tokens and
  cost, affected resources linked to the Map, memory reuse, outcome; the root cause with each
  claim opening to the exact evidence lines it cites; what changed (deploys, commits, secret
  changes on the window, first signal marked); fixes with PR links and state; the last
  recovery check; research links; Stop; then the live Medic run timeline.
- Shared timeline: Medic events render with `MedicStep`; fix approvals with `FixApprovalCard`
  (emergency actions as before -> after lines, PRs as their diff; "Approve and run" /
  "Approve and open PR"). `use-run-stream` now subscribes to the incident events (it only
  listened to deploy events, so Medic steps would never have arrived live).
- Tests: `components/incidents/incidents.test.tsx` (10). `tsc`, eslint, vitest (91), `next build`.

The Connections page's "Emergency fixes" toggle landed separately (`phase5-operate-toggle`):
"Enable emergency fixes" on a connected AWS card walks through the stack update
(`EnableOperate=true`, `OperateServiceArns` and `OperateSecretArns` prefilled with the account),
then Verify calls `POST /connections/{id}/aws/operate`; an "Emergency fixes" badge shows when on.

## Live bad-deploy-oom: rollback PassRole, retry (2026-10-14)

The emergency rollback failed with AccessDenied: `groundcrew-operate` may not `iam:PassRole` on
`role/checkout-worker-task-execution`. ECS checks PassRole on a revision's execution and task
roles when UpdateService points a service at it.
- Template (and API copy): parameter `OperatePassRoleArns` (CommaDelimitedList, default
  `arn:aws:iam::*:role/none`); the operate role gets `iam:PassRole` on it with
  `iam:PassedToService = ecs-tasks.amazonaws.com`, plus an explicit Deny on PassRole for
  `role/groundcrew-*`. No other IAM action. `EnableOperate=false` renders exactly as on main for
  every Tier x CreateResourceExplorerIndex (checked against `git show main:`).
- Checked without changing anything in AWS (own CLI credentials, account 881050425883,
  us-east-2): `cfn-lint` clean; `validate-template --template-body` accepts it; Access Analyzer
  `ValidatePolicy` on the rendered operate policy: no findings; `SimulateCustomPolicy`:
  PassRole `checkout-worker-task-execution` / `checkout-api-task` to ecs-tasks allowed; to
  lambda, ec2, or ecs implicitDeny; `groundcrew-task-execution` and `groundcrew-operate`
  explicitDeny; `admin`, `payments-task-execution` implicitDeny; `iam:CreateRole`,
  `AttachRolePolicy`, `PutRolePolicy` implicitDeny.
- `plan_ecs_rollback_service` checks the target revision's roles against the operate role's
  PassRole scope first and stops with "rollback needs PassRole on <role>; update the connection".
- A failed emergency fix shows AWS's error on the incident page and a Retry button: the fix is
  planned again and needs a new approval on the same run (`POST
  /incidents/{id}/fixes/{fix_id}/retry`, `app/agents/medic/retry.py`).
- Enable emergency fixes dialog: `OperatePassRoleArns` with the demo value.
- "4 issues" dev badge on the incident page: new, not from #34 (merged; it fixed the verify-row
  keys). Tavily returned the same runbook URL twice, and the Research section and the timeline's
  research step keyed by URL (2 lists x 2 renders). Keys now include the index, and Tavily results
  are de-duplicated by URL. Reloaded in Chrome: no console errors.
- To use it on the demo: update the `groundcrew-read-only` stack with
  `OperatePassRoleArns=arn:aws:iam::881050425883:role/checkout-*` (after the template is
  republished with `make publish-connect-template`), then Retry the failed rollback.
- Files (Ahmed's Medic code marked *): infra/aws-connect.yaml, app/connectors/templates/
  aws-connect.yaml, app/actions/{aws,base}.py, app/agents/medic/retry.py*, app/routes/
  incidents.py, app/tools/tavily.py*; tests/test_aws_connect_template.py,
  tests/test_actions_aws.py, tests/test_medic_retry.py; web components/connections/
  aws-operate-dialog.tsx, components/incidents/{fix-list,medic-step}.tsx*,
  app/(app)/incidents/[id]/incident-view.tsx*, lib/{incidents,timeline}.ts, lib/api/*.

## Follow-ups: the permanent fix after a rollback resolved (2026-10-14)

Live bad-deploy-oom: "checkout-worker: exit code 137" showed Resolved after the rollback while
the fix PR (checkout-demo #3) was still open, so the next deploy would have brought the outage
back and nothing said so.
- Model: `incident_followups` (kind `fix_pr`, state open | merged | closed_unmerged | deployed,
  PR ref, triggering commit, merge commit, deploy run, times) and `incidents.remembered_at`;
  migration `b7d1e3f5a9c2`, single head, with a backfill from stored fix states.
- Medic's resolve creates one per applied fix PR not deployed yet and records `incident_updated`
  with `followups`. The watcher job tracks them read-only through the GitHub App (each PR at most
  every 2 min, for 7 days): merged -> deployed (a successful deploy workflow run on the base
  branch containing the merge commit), or closed_unmerged.
- Memory: an incident is found only once `remembered_at` is set (resolve without an open PR, or
  the PR merged or closed); a closed PR's fix is reported as "rollback only".
- API: `IncidentDetail.followups`, `has_open_followups`, `GET /incidents?pending_fix=`,
  `WorkspaceSummary.pending_permanent_fixes`, `IncidentUpdated.followups`.
- UI: notes under the incident's outcome (amber "Permanent fix pending: PR #n not merged yet",
  amber "PR #n merged, deploying...", green "Permanent fix deployed", red closed-without-merge
  warning naming the commit); "fix pending" badge and a "Fix pending" tab on the list
  (`/incidents?pending_fix=true`); an overview banner with the count.
- Local dev DB: migrated; the backfill found 3 resolved incidents with merged fix PRs
  (checkout-demo #1, #2, #3). One tracker pass (GitHub reads only) found all three deployed by
  workflow run 38520627437; the incident page shows "Permanent fix deployed" with no console
  errors.
- Not checked: the ECS deployment of the new revision itself; the deploy workflow's success
  stands for it. Tracking runs only while INCIDENT_WATCH_ENABLED=true (watcher schedule).
- `change_in_repo` follow-ups: resolved by a rollback with no fix PR open (rejected or never
  proposed): red "Running on a rollback; the repo still contains commit <sha>, so the next deploy
  will bring the failure back." Done when the newest successful deploy after it has every file
  the commit touched as in the commit's parent (reverted), or when a person dismisses it
  (`POST /incidents/{id}/followups/{followup_id}/dismiss`, a Dismiss button on the note). Not
  backfilled for incidents resolved before this.
- Files (Ahmed's Medic / incidents code marked *): app/db/models.py, app/db/migrations/versions/
  20261014_b7d1e3f5a9c2_incident_followups.py, app/incidents/followups.py (new)*,
  app/incidents/{views,watcher}.py*, app/agents/medic/{agent,events,memory}.py*,
  app/routes/{incidents,incident_schemas,schemas,workspaces}.py; tests/test_followups.py (new),
  tests/test_medic.py* (bad-deploy-oom now merges the PR before memory reuses it); web
  components/incidents/{followup-note,incident-list}.tsx*, components/incidents/
  {followup-note,incidents-home}.test.tsx, components/incidents/incidents.test.tsx*,
  app/(app)/incidents/{page,incidents-home}.tsx*, app/(app)/incidents/[id]/incident-view.tsx*,
  app/(app)/page.tsx, lib/incidents.ts, lib/api/*, tests/incident-fixtures.ts,
  tests/discovery_helpers.py (routes may answer per request).

## Live broken-env rerun: merged, down without health checks, verify, research, memory (2026-10-14)

Incidents "checkout-worker: 5 error lines" and "checkout-api: 6 error lines". The secret rollback
and restarts worked (errors normal on both), but the run looked like two failures.
1. Merged: the worker's incident failed with "No fix" at 06:50:40; the api's signal at 06:51:10
   opened a new incident that took it over, and the list showed both as Not resolved. A taken-over
   incident now gets status `merged` (`merged_into` in its state; `merged_from` on the new one),
   linked both ways on the incident page, hidden from the default list, shown under "Merged".
2. Down without health checks: `Signal.down` when >= 90% of a service's log lines are errors for
   2+ minutes (>= 3 lines; the worker's every poll failed). `fixes.is_down` treats it like a
   failing health check (emergency fixes, rollback of the crashing service). "No fix" now waits
   `MEDIC_NO_FIX_GRACE_SECONDS` (90) for related signals (`medic_no_fix` job); a signal from a
   new resource (or newly down) is material, joins the incident, and re-diagnoses it.
3. Verify: ECS keeps old service events, so "(task 825dad...) failed container health checks"
   from the task the restart replaced counted for 14 rounds. `verify.current_health_events`
   counts only events after the fix and after the primary deployment started, and not about a
   task that has stopped.
4. Research: the evidence line holding the worker's traceback is cut short, so `json.loads`
   failed and the raw JSON was searched. Broken JSON now yields its `msg` and the last exception
   named in it. Every path uses the dominant error (app logs, then any source, preferring lines
   that name an exception). Queries carry the app's stack (`stack_terms`: from its lines, the
   graph's RDS engine, and the diagnosis, e.g. "postgres asyncpg sqlalchemy"), and results that
   share no key term, or are about another database (SQL Server for a Postgres app), are dropped.
5. Memory (fallback, no PRICE_EMBED_PER_M): when a past incident's triggering change was made to
   the same thing again in this window (any version of secret checkout/database-url), it counts
   for half the score and rules/resources use containment: the live pair now scores 0.95 (was
   0.79); unrelated incidents unchanged. The timeline says "(best 79% similar)" when none is
   close enough.
6. Secret version ids in full: the plan summary said "back to version terrafor" (8 characters of
   a Terraform id); the Map's `version_stages` keys too.
- Tests: tests/test_live_fixes_4.py (9); updated tests/test_live_fixes_3.py (fixed resource id
  in `_sig`), tests/test_live_missing_index.py (query carries the stack), tests/test_medic.py
  (realistic Tavily snippet), tests/test_actions_aws.py, tests/test_aws_discovery.py; web
  incidents.test.tsx (+2), incidents-home.test.tsx (+1).
- Files (Ahmed's code marked *): app/agents/medic/{agent,events,fixes,jobs,memory,state,
  verify}.py*, app/incidents/{rules,store,views}.py*, app/tools/tavily.py*, app/actions/aws.py,
  app/discovery/aws.py, app/config.py, app/worker.py, app/routes/{incidents,incident_schemas}.py;
  web lib/incidents.ts, app/(app)/incidents/incidents-home.tsx*, app/(app)/incidents/[id]/
  incident-view.tsx*, components/incidents/medic-step.tsx*, lib/api/*.

## Marking your own tooling: groundcrew:role (2026-10-14)

checkout-demo's checkout-vercel-api-url Lambda (and its role, log group, rule, and the
demo-tooling/vercel-token secret) is demo tooling, tagged `groundcrew:role=tooling`.
- `app/graph/roles.py`: the AWS tag `groundcrew:role` or the Kubernetes label `groundcrew/role`
  (`tooling` or `app`, case-insensitive) decides before the name rules; `app` keeps a
  tooling-looking name in the app. The name rules stay as the fallback.
- Map: `ResourceOut.role` uses the tags, so tagged tooling folds into the collapsed Tooling group.
- Summary: `digest` leaves out tooling that no edge connects to an app resource (edges among
  tooling, e.g. the Lambda to its secret, do not count); `summary_check` uses the same rule.
- Medic: `telemetry.collect.load_resources` skips tooling neighbors; tooling the incident names
  directly is still read.
- Discovery: Lambda tags from `lambda:ListTags`, IAM role tags from `iam:GetRole` (ListFunctions
  and ListRoles leave tags out); log groups, EventBridge rules, and other inventory types get
  their tags from Resource Explorer or the tagging API; secrets from ListSecrets. Both calls are
  already in the read-only role (infra/aws-connect.yaml): no template change, no republish.
  `lambda:ListTags` was added to the code allowlist (`AWS_READ_OPERATIONS`).
- Tests: tests/test_tooling_role.py (7); tests/test_edges.py (the digest leaves tooling out instead
  of noting it); tests/aws_fixture.py (`iam:GetRole`).
- Files (Ahmed's code marked *): app/graph/{roles,digest,summary_check}.py,
  app/routes/discovery_schemas.py, app/discovery/aws.py, app/connectors/aws.py,
  app/telemetry/collect.py*, README.md.

## Live broken-env follow-ups: kept approvals, what changed, first signal (2026-10-14)

Incident c9c38b96 (see also #40, verify):
2. 11:03:49 new signals re-diagnosed the incident; it proposed the same three fixes, but their
   approvals had already been rejected ("superseded"), so they were approved twice. Re-diagnosis
   now parks the waiting fixes (`MedicState.superseded`, approvals still pending); identical
   fixes (`_same_fixes`: action and plan hash or PR draft hash, in order; wording ignored) are
   kept with their ids and approvals, and a decision made meanwhile resumes the run. Different
   fixes, no fix, resolve, or fail close the parked approvals as before.
3. "What changed" was mostly "checkout-demo deployment READY": Vercel redeploys (`source:
   redeploy`, same commit 5f63808) made by the API_URL Lambda every few minutes.
   `telemetry.collect` drops change markers on tooling resources (`app.graph.roles`) unless the
   incident names them; the Vercel reader skips deployments with meta `groundcrew:role` /
   `groundcrew_role` = `tooling` and labels redeploys "redeploy READY (same source, commit ...)";
   the strip folds back-to-back repeats into one row ("×3, last ..."). The Lambda does not set
   that meta yet (a checkout-demo change, to verify against Vercel's API before it ships).
4. The strip's "First signal" was the earliest signal (10:13, from the incident it took over);
   it now uses `first_signal_at`, the opening signal's onset (10:57:59), exposed on the API.
- Tests: tests/test_live_fixes_5.py (7), tests/test_live_fixes_3.py (the same draft keeps its
  approval); web change-strip.test.tsx (2).
- Files (Ahmed's code marked *): app/agents/medic/{agent,state}.py*, app/telemetry/{collect,
  vercel}.py*, app/connectors/vercel.py, app/incidents/views.py*, app/routes/incident_schemas.py;
  web components/incidents/change-strip.tsx*, app/(app)/incidents/[id]/incident-view.tsx*,
  lib/api/*.
## Verify: health from task records, not service events (2026-10-14)

Live broken-env, incident c9c38b96 (with #38 running: memory matched at 0.95). The fix (secret
rollback + restarts) landed at 11:05:03; checkout-api's new task 602e5a1d was HEALTHY, yet every
round from 11:06:50 to the deadline said "1 health-check failures reported by ECS", and the
incident failed.
- Source: the ECS **service event** "(task a759e4db...) failed container health checks" at
  11:05:56. a759e4db started at 11:04:10 on the broken secret (deployment ecs-svc/3472...) and
  was stopped for health at 11:06:34: a task the fix replaced. #38 counted service events after
  max(fix, primary deployment start) unless the task was in the stopped list, but that list is
  one `ListTasks STOPPED` page capped at 50 in no set order; a759e4db was not on it (the detail
  also said "0 tasks failed health checks since the fix", so verify never saw the task). Not
  the cause: task healthStatus (602e5a1d HEALTHY) or the deployment's failedTasks (0).
- Fix (`app/agents/medic/verify.py`): `service_tasks` reads the service's running tasks and
  describes every task a health event since the fix names (ECS keeps stopped tasks describable
  for an hour); events only point at tasks and are never counted. `health_failures` counts tasks
  *started* since the fix that stopped for health, and running tasks (desired RUNNING) that are
  UNHEALTHY now. "Exited non-zero" counts only tasks started since the fix too (a replaced task
  exits non-zero on its way out). Details: "N tasks started since the fix failed health checks,
  M running tasks unhealthy", "N tasks started since the fix exited non-zero".
- Checked against live AWS through Groundcrew's read-only role, since = 11:05:03: rollout
  COMPLETED, 0 health failures, 0 unhealthy, 0 exits: all pass.
- Not changed: the watcher's detection rule (`rules.ecs_signals`) still reads service events over
  the last 5 minutes; `_closed_at` keeps a resolved incident from reopening on them.
- Tests: tests/test_live_fixes_4.py (the live shape: the replaced task off the stopped page,
  named only by the event; the fix's own task failing, stopped or running; a draining task).
- Files (Ahmed's code marked *): app/agents/medic/verify.py*, tests/test_live_fixes_4.py.

## How to resume

```bash
cd ~/Personal/groundcrew && git fetch && git checkout <branch>
make db
cd apps/api && UV_PROJECT_ENVIRONMENT=~/Personal/envs/groundcrew uv run pytest -q -m "not docker"
```

## Connect template fix: Rules functions (2026-10-13)

The published template failed in CloudFormation: "Following functions are not supported in the
Rules block of the template: [Fn::Join]". `cfn-lint` 1.57.2 did not catch it; `validate-template`
does. Fixed:
- `OperateNeedsServicesAndSecrets` asserts `!Not [!Contains [<list>, <placeholder ARN>]]` instead
  of joining the list. Same effect (and it also refuses the placeholder mixed with real ARNs).
  Only the Rules block changed; every Tier x CreateResourceExplorerIndex x EnableOperate
  combination renders exactly as before. API copy synced.
- `test_rules_use_only_supported_functions`: any `Fn::` under Rules outside Equals, Not, And, Or,
  Contains, EachMemberEquals, EachMemberIn, RefAll, ValueOf, ValueOfAll fails the test.
- `make publish-connect-template` now runs the template tests, `cfn-lint`, uploads to
  `staging/aws-connect.yaml`, runs `validate-template --template-url` on it, and only then copies
  it to the live key. Any failure stops before the live key is written.

Checked without changing anything in AWS: `validate-template --template-body` rejects the old
template with the error above and accepts the new one; `cfn-lint` clean; Access Analyzer
`ValidatePolicy` has no findings for any role's permission or trust policy (Tier=deploy,
EnableOperate=true); `SimulateCustomPolicy` on the operate role: UpdateService allowed on
`service/checkout/*`, implicit deny elsewhere and for DeleteService / RegisterTaskDefinition;
UpdateSecretVersionStage allowed on `secret:checkout/*`, implicit deny on other secrets and for
PutSecretValue; GetSecretValue explicit deny.

## Live tuning from checkout-demo runs (2026-10-13)

Rizwan, in Ahmed's telemetry / incidents / Medic code (agreed with the user; review welcome):
- RDS `[SPIKE]` needs an absolute floor too: CPU 30 %, ReadLatency 20 ms (2.4 ms vs 0.0 ms was a
  spike). The old `rds_cpu_latency` fallback needs 20 ms read latency (was 2 ms).
- Latency summaries: request logs without a path are skipped (no more `path= requests=6`); a
  minute at 3x the path's baseline p50 and >= 20 ms is marked `[SLOW]`; health-check paths
  (`/health`, `/healthz`, `/ready`, ...) are summarized but never marked or weighted.
- Slow-query rule (keeps the `rds_cpu_latency` name, no contract change): 2 minutes in a row with
  DB CPU > 15 % and a non-health path's p50 at 3x baseline, from the request logs of services
  that `reads` the database. Live missing-index numbers (p50 9 -> 48 ms, CPU 4.5 -> 20 %) fire;
  p95 outliers, /health jumps, one slow minute, or CPU 10 % do not. Watcher and verify both use
  `rules.rds_check`.
- Logs are read newest first (`cloudwatch_logs.read_newest`, 2-minute slices from the end,
  halved when full): a busy 40-minute window keeps the last ~10 minutes, not the first, and says
  what it did not read. The watcher's error-rate baseline uses only the part that was read.
- Secret versions show the full version id (Terraform ids all start `terraform-`, so the first
  8 characters were `terrafor` for every version).
- "no reader for ..." goes to `Evidence.skipped`, not `notes`, so it never reaches Medic.
- Deploys get a line at their start (ECS deployment `createdAt`, workflow run `created_at` with
  its sha), and the diagnosis prompt says a deploy acts from its start (a migration runs before
  the new tasks are live).
- tests/test_live_tuning.py (18).

## broken-env run #2 fixes (2026-10-13)

- Fix dependencies: `ProposedFix.depends_on`; the API refuses approving a restart before the
  secret rollback is applied, the timeline keeps its Approve button disabled ("Runs after Fix
  1: ..."), and a restart whose rollback was rejected or failed is closed without running. (In
  run #2 the restarts did run after the rollback, 08:59:08.6 then 09.9 and 11.6, because Medic
  applies in order; a rejected or failed rollback was the real gap.)
- One outage, one incident: the worker's incident failed ("No fix": an error spike is not
  "down") a minute before the api's health checks opened a second one. A related incident that
  failed in the last 30 minutes (not stopped by a person) is now taken over by the next one
  (`merged_from` / `merged_into`, linked both ways in the timeline).
- Running is not healthy: the health_check signal reads "N health-check failures reported by
  ECS (1/1 tasks running, not healthy)" (was "0 tasks stopped failing health checks"), the ECS
  "now:" line says so, the diagnosis prompt forbids calling a service recovered from its task
  count, and verify fails `health` on ECS health-check failure events since the fix.
- Redaction keeps AWS ARNs and secret identifiers (`secret_id=`, `SecretName`), so Medic can
  name and roll back the changed secret (run #1's "No fix"); values stay redacted.
- Research queries a JSON log line by its `msg` and error (last traceback line), not the raw
  JSON.

## missing-index run fixes (2026-10-13)

Root cause was right (b11275e, migration at deploy start); three gaps:
- No fix PR: the model named the deploy by its run id (`38384663137`); the SHA regex read its
  eleven digits as a SHA prefix, found no commit, and Medic said the repository was not
  connected. A SHA now needs a hex letter; `fixes.commit_change` maps a deploy marker to the
  commit it shipped (`ChangeMarker.commit`), and `fixes.resolve_repo` takes the repo the GitHub
  evidence names (`ChangeMarker.repo`), then the graph, matched to the workspace's GitHub
  connections, re-reading each installation's repository list when none lists it. Notes now
  say which step failed.
- "Detected in 24m 1s": the incident merged the seed-large load test's failed incident and
  took its first signal. Detection is now timed from the opening signal, moved to the
  triggering change when that is later. The diagnosis check drops `[SPIKE]` lines whose
  minute ended before the triggering change (and the prompt says so).
- Research: the first query is the application logs' most frequent error (`dominant_error`),
  not the top-scored line (a health check); one follow-up search runs on
  `RootCause.search_query` after the diagnosis and goes to the fix PR prompt as background.
- Files: app/agents/medic/{agent,diagnose,events,fixes,schemas}.py, app/incidents/store.py,
  app/telemetry/github.py, app/tools/tavily.py; tests/test_live_missing_index.py (6),
  tests/test_incidents_detection.py (merge now keeps the opening signal's time).

## Live updates and Medic diagnosis fixes (2026-10-13)

- Live updates: with `make dev KIND=1` the api joins the kind network (IPv6), so Docker also
  published `[::1]:8000` and forwarded it to the container's IPv6 address, where uvicorn (IPv4
  only) does not listen. Chrome tries `localhost` as ::1 first: the browser's SSE to
  `NEXT_PUBLIC_API_URL=http://localhost:8000` was reset (503 in DevTools) while REST, through
  the web proxy, worked. The api port is now published on IPv4 only (`0.0.0.0:8000:8000`).
  `useRunStream` also polls `GET /runs/{id}` every 5 s while the stream is down. Lists already
  poll every 10 s. Verified in Chrome: a discovery run and a reported incident updated live.
- The "1 issue" dev badge: duplicate React keys (`errors-`) in verify rows, where Medic lists
  an `errors` check per service.
- Error lines are classified by level (`app/telemetry/log_levels.py`), never by logger name:
  uvicorn's startup and shutdown are "uvicorn.error" at INFO and opened an incident on every
  deploy. Used by the spike rule, evidence selection, and the research query.
- "False alarm" must pass live checks (error rate, health, latency; the databases the services
  read are now checked too); otherwise Medic diagnoses again with what failed.
- New material signals while fixes wait for approval supersede them and re-diagnose; identical
  signals are stored once (the live incident had the same signal 4 times).
- Fix PR titles name the change ("Recreate orders.customer_email index concurrently").
- Verify: one chain per incident (a second resume started a second chain: every round twice),
  rounds wait during a rollout IN_PROGRESS or until a merged fix PR's deploy starts, and the UI
  shows "Waiting for ..." / "Not recovered yet (check n)", collapsing repeats.
- Files: app/telemetry/{log_levels,cloudwatch_logs}.py, app/incidents/{rules,store,views}.py,
  app/agents/medic/{agent,diagnose,fixes,jobs,schemas,state,verify}.py, app/pipeline/events.py,
  app/tools/{github_pr,tavily}.py, app/worker.py, docker-compose.yml; web
  lib/{use-run-stream,incidents}.ts, components/deploy/deploy-step.tsx, components/runs/
  timeline.tsx. Tests: tests/test_live_fixes_3.py (10), components/incidents/
  live-updates.test.tsx (3).
- Test incidents: `incidents.is_test` (migration a4f2c7e9b1d6), `test` on `POST /incidents`,
  excluded from memory (`memory.similar`, no embedding) and from dedupe and merging. The local
  dev-session report "Reported problem on checkout-api" (2b939290) was marked `is_test`. Files:
  app/db/models.py, app/db/migrations/versions/20261013_a4f2c7e9b1d6_incident_is_test.py,
  app/agents/medic/{memory,agent}.py, app/incidents/{store,views}.py,
  app/routes/{incidents,incident_schemas}.py; web report-dialog.tsx, incident-list.tsx.
