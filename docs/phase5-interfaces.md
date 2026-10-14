# Phase 5 interfaces (Medic)

The contract between Medic (Ahmed: telemetry, incidents, `app/agents/medic/`, the incidents API)
and Rizwan (Incidents UI, the Operate role, `app/actions/`). Both sides build against this file
and the code it names. M0 lands the types, the 501 routes, and fakes for both sides; nothing here
talks to AWS or GitHub yet.

Status: **agreed** with Rizwan on 2026-10-12 (his answers are in [Decisions](#decisions)).

## How an incident runs

```
watcher (60 s) or "Report incident" ──▶ POST /incidents ──▶ incidents row + Medic run (agent="medic")
  gather   collect_evidence(affected + graph neighbors, window)      evidence_collected
  memory   similar past incidents (pgvector)                          memory_checked
  research Tavily on the most distinctive error (max 3 results)       research_done
  diagnose root_cause route -> RootCause, uncited claims dropped      diagnosis
  propose  fix PR always; emergency action when prod is down now      fix_proposed
  approve  one approval per fix (POST /approvals/{id}/decide)         approval_requested / approval_decided
  apply    open_fix_pr / EmergencyActions.execute with the token      fix_applied
  verify   re-check the signals that opened it, until healthy/timeout verify_result
  remember summary, root cause, fix, embedding on the incident        incident_resolved | incident_failed
  every status change                                                 incident_updated
```

An incident is investigated by a normal run, so the UI reuses what deploys use: the run stream
(`POST /runs/{run_id}/stream-token`, `GET /runs/{run_id}/events`), the approvals inbox, Stop, and
the model badge, tokens, and cost on every `llm` step.

## 1. Evidence (Ahmed, `app/telemetry/`)

```python
async def collect_evidence(
    workspace_id: uuid.UUID, resource_ids: list[uuid.UUID], since: datetime, until: datetime
) -> dict[str, str]
```

- Keys are `<reader>:<resource>`: `cloudwatch_logs:/ecs/checkout-api`, `ecs:checkout-worker`,
  `rds:checkout-orders-db`, `secrets:checkout/database-url`, `github:GroundsCrew-AI/checkout-demo`,
  `vercel:checkout-web`, and `timeline` (everything merged in time order).
- Values are lines `ISO-timestamp | source | resource | message`, oldest first, redacted, capped
  per source with a `... N more lines` marker (`evidence_line()` in `app/telemetry/evidence.py`).
- Medic turns the ranked lines into **evidence items** `{id, source, timestamp, resource_id, line}`
  (`EvidenceItem`, `app/agents/medic/schemas.py`). `id` is stable (`evidence_id()`: `ev-` plus 10
  hex characters of a hash of source, timestamp, and line), so the model, the stored incident, and
  the UI all cite the same line by the same id.
- Read-only: AWS only through `CredentialsProvider.aws_session()` (read-only role,
  `DISCOVERY_REGIONS`), never a secret value, never customer data. Every AWS call it needs is
  already in `AWS_READ_OPERATIONS` and the read-only role (`logs:FilterLogEvents`,
  `cloudwatch:GetMetricData`, `ecs:Describe*` / `ListTasks`, `secretsmanager:DescribeSecret`, which
  also returns version ids and stages). No template change needed.
- Neighbors come from the graph: `uses` (secret), `logs_to` (log group), `reads` (database),
  `deployed_by` / `defined_in` (workflow, repo, commit), `calls` (callers).
- Tests use `FakeEvidence` (same module). Dev-only `GET /workspaces/{id}/evidence?resource=&since=`
  arrives with the readers (M1).

## 2. Emergency actions (Rizwan implements, Medic calls; `app/actions/`)

Contract: `app/actions/base.py`. Fake with the real approval checks: `app/actions/fake.py`
(`FakeEmergencyActions`), which Medic uses until the real one lands (target Oct 17).

Each action is a read-only plan, then an approved execute (the same split as
`Target.rollback_plan()` / `rollback()`), so the approval card shows exactly what changes:

```python
class EmergencyActions(Protocol):
    async def plan_ecs_rollback_service(self, connection_id, cluster, service) -> ActionPlan
    async def plan_secret_rollback_version(self, connection_id, secret_arn) -> ActionPlan
    async def plan_force_redeploy(self, connection_id, cluster, service) -> ActionPlan
    async def execute(self, plan: ActionPlan, token: ApprovedAction, *, run_id) -> ActionResult
```

| Action | Plan reads | `current` -> `desired` | Execute calls |
|---|---|---|---|
| `ecs_rollback_service` | `DescribeServices`, `ListTaskDefinitions` (family) | task definition ARN now -> previous revision's ARN | `UpdateService(taskDefinition=desired)` |
| `secret_rollback_version` | `DescribeSecret` (`VersionIdsToStages`) | version id holding `AWSCURRENT` -> the one holding `AWSPREVIOUS` | `UpdateSecretVersionStage(AWSCURRENT, MoveToVersionId=desired, RemoveFromVersionId=current)`. Never reads the value |
| `force_redeploy` | `DescribeServices` | primary deployment id -> null | `UpdateService(forceNewDeployment=True)` |

Rules for `execute`:

1. Call `consume_approved_action(session_factory, token, run_id=run_id, action=plan.action,
   plan_hash=plan.plan_hash())` before the first write. Missing, reused, or mismatched tokens raise
   `PolicyViolationError`.
2. Re-read the state; if it no longer equals `plan.current` (for example the service's task
   definition changed), return `ok=False` ("the state changed since the plan was approved") and
   write nothing.
3. Return `ActionResult(ok, summary, details={...ids...}, error)`; do not wait for the service to
   become stable (Medic's verify does that).
4. AWS only through `CredentialsProvider.aws_operate_session(connection_id)`.

`ActionPlan.plan_hash()` covers action, connection, target, params, current, desired (not the
wording or the time). `EmergencyActionError` (safe message) when no plan is possible: no previous
revision, no `AWSPREVIOUS`, no `operate` capability, or a resource outside the allowed ARNs.

**How Medic finds the real implementation:** `app/actions/__init__.py` exposes
`get_emergency_actions(session_factory, settings) -> EmergencyActions | None` (None when the
Operate role is not set up). Medic calls it if it exists; until then it offers no emergency action
and says so on the incident ("no Operate role"). Tests inject `FakeEmergencyActions`.

### Operate capability (built 2026-10-12)

- Stack parameter `EnableOperate` (`true` / `false`), independent of `Tier=deploy`: a customer can
  allow emergency fixes without allowing deploys. A connection stores its capabilities as a set of
  `observe`, `deploy`, `operate`.
- Role `groundcrew-operate`, same trust and ExternalId as the others. Stack parameters
  `OperateServiceArns` (`arn:aws:ecs:...:service/<cluster>/<service>`, wildcards per cluster) and
  `OperateSecretArns` (prefix wildcards). Demo: `service/checkout/*` and `secret:checkout/*`.
- Allowed: `ecs:UpdateService`, `ecs:DescribeServices` on `OperateServiceArns`;
  `secretsmanager:UpdateSecretVersionStage`, `DescribeSecret` on `OperateSecretArns`;
  `ecs:ListTaskDefinitions`, `DescribeTaskDefinition` on `*` (read-only, not scopable);
  `iam:PassRole` on `OperatePassRoleArns` only with `iam:PassedToService =
  ecs-tasks.amazonaws.com` (2026-10-14: UpdateService to another revision needs PassRole on that
  revision's execution and task roles; the live bad-deploy-oom rollback failed with AccessDenied
  without it). Demo: `arn:aws:iam::881050425883:role/checkout-*`. The default
  (`role/none`) matches nothing. Explicit Deny on `iam:PassRole` for `role/groundcrew-*` and on
  secret values. No other IAM action. `force_redeploy` keeps the same revision, so it passes no
  role.
- `plan_ecs_rollback_service` reads the target revision's `executionRoleArn` / `taskRoleArn` and
  the operate role's PassRole scope (its inline policy, via the read-only role's
  `iam:GetRolePolicy`). A role outside it stops the plan with `EmergencyActionError`
  "rollback needs PassRole on <role>; update the connection ..." (Medic notes it; no approval is
  requested). If the policy cannot be read, the plan goes ahead and AWS decides at execute.
- `aws_operate_session(connection_id)` has a runtime allowlist (`AWS_OPERATE_OPERATIONS`) and
  refuses unless the connection has the `operate` capability. `EnableOperate=true` with the
  placeholder (default) ARN lists is refused by a template Rule.
- Turning it on: update the `groundcrew-read-only` stack with `EnableOperate=true`,
  `OperateServiceArns`, `OperateSecretArns`, and `OperatePassRoleArns`; then `POST /connections/{id}/aws/operate`
  `{"enabled": true}` verifies the role (ExternalId required) and stores `operate_role_arn`.
  `ConnectionOut.capabilities` lists `observe`, `deploy`, `operate`. `{"enabled": false}` stops
  using the role at once.
- `make plan-emergency CONNECTION=<id>` prints the real plans for a service and secret, read-only.
- Implementation: `app/actions/aws.py` (`AwsEmergencyActions`), found by Medic through
  `app.actions.get_emergency_actions`.

## 3. Fix pull requests (Ahmed, `app/tools/github_pr.py`)

```python
class FixPullRequests(Protocol):
    async def open_fix_pr(self, draft: PullRequestDraft, token: ApprovedAction, *, run_id) -> PullRequestRef
```

`PullRequestDraft` holds `installation_id, repo, base, branch, title, body, files: dict[path,
content]` (the plan's `open_fix_pr(installation_id, repo, branch, files, title, body)` as one
approvable object). It always opens a new `groundcrew/fix-*` branch from `base` and a PR: never a
commit to the default branch, never a merge. The approval binds `draft.plan_hash()` (file
contents included). `PullRequestRef` is `{repo, number, url, branch, head_sha, state: open |
closed | merged}`; `state` is refreshed from GitHub when an incident is read.
`FakeFixPullRequests` records PRs with the real approval check. The GitHub calls land in M3 in
Medic's own module, with a write allowlist of its own (refs under `groundcrew/fix-*`, git
blobs/trees/commits, pulls); `app/connectors/github.py` stays GET only.

GitHub App (Rizwan owns it): Contents and Pull requests are Read and write. checkout-demo `main`
gets branch protection, so a fix PR needs a review before it can merge.

## 4. Approvals and tokens (`app/policy/actions.py`, small change in this PR)

Medic creates one approval per fix in its run (`tool_call_id = "medic:<incident>:<fix_id>"`,
`arguments = {"plan_hash": ..., "fix_id": ...}`) and, after approval, gets a token with
`issue_approved_action(...)`. This PR widens the token's action names; each keeps its policy
level and is bound to plan hash and run (no behaviour change for targets):

| Tool name | Policy | `plan_hash` of |
|---|---|---|
| `open_fix_pr` | write | `PullRequestDraft` |
| `ecs_rollback_service` | emergency | `ActionPlan` |
| `secret_rollback_version` | emergency | `ActionPlan` |
| `force_redeploy` | emergency | `ActionPlan` |

Resume: Rizwan's Phase 4 M6 makes `resume_run` pick a handler registered by `run.agent` (default
`AgentRunner`). Medic registers `"medic"` in M4.

## 5. RootCause (Ahmed, `app/agents/medic/schemas.py`)

Structured output of the `root_cause` task (Ultra/on, Super with `DEV_MODE_NO_ULTRA=true`). The
model sees each evidence line with its id and cites ids:

```python
class RootCause(BaseModel):
    summary: str
    root_cause: str
    claims: list[Claim]                          # {text, evidence_ids}
    triggering_change: TriggeringChange | None   # {kind: commit|secret_change|deploy|config|other, ref, at, description, evidence_ids}
    affected_resources: list[str]
    confidence: Literal["high", "medium", "low", "other"]
    fix_options: list[FixOption]                 # {kind: pull_request|ecs_rollback_service|secret_rollback_version|force_redeploy|other, description, target}
```

After the call Medic removes every id that is not in the incident's evidence items, then drops
each claim (and the triggering change) left with no id; what it removed is listed in
`Diagnosis.dropped`. The UI links each claim to its lines through `IncidentDetail.evidence`.
`fix_options` are suggestions: Medic builds the concrete PR draft or `ActionPlan` itself and never
runs model text as an action.

## 6. Incidents API (Ahmed; Rizwan's UI)

Code: `app/routes/incidents.py` (live since M4, behind auth and membership like `/deploys`),
`app/routes/incident_schemas.py`. Types are in `apps/web/lib/api/schema.d.ts`.

| Method | Path | Body / query | Response |
|---|---|---|---|
| POST | `/incidents` | `IncidentCreate` | 202 `IncidentCreated` `{incident_id, run_id, deduped}` |
| GET | `/incidents` | `workspace_id`; filters `status`, `severity`, `resource` (resource id), `since` (opened at or after), `pending_fix` (true: a follow-up is open or merged; false: none is), combined with AND; `limit` (1..100, 25), `cursor` | 200 `IncidentPage` `{items, next_cursor}`, newest first |
| POST | `/incidents/{incident_id}/fixes/{fix_id}/retry` | | 200 `IncidentDetail` (a failed emergency fix planned again with a new approval) |
| POST | `/incidents/{incident_id}/followups/{followup_id}/dismiss` | | 200 `IncidentDetail`; 409 unless an open `change_in_repo` follow-up |
| GET | `/incidents/{incident_id}` | | 200 `IncidentDetail` |

**Report incident** (`IncidentCreate`, unknown fields are a 422):

```json
{
  "workspace_id": "6f0c...",
  "resource_ids": ["9a7d..."],
  "title": "Orders stay pending",
  "description": "New orders do not leave pending since about 10:00",
  "severity": "high",
  "since": "2026-10-19T09:30:00Z"
}
```

`resource_ids` has 1 to 20 graph resources; `title`, `description`, `severity` (default from the
signals), and `since` (default 60 minutes before the report) are optional. One open incident per
group of related resources: reporting resources an open incident covers returns that incident with
`deduped: true`.

**`IncidentSummary`** (list rows):

| Field | Meaning |
|---|---|
| `id`, `workspace_id`, `run_id` | `run_id`: the latest Medic run for it |
| `title`, `status`, `severity` | status `open`, `investigating`, `waiting_approval`, `applying`, `verifying`, `resolved`, `failed`, `merged` (a newer incident on a related resource took it over; hidden from `GET /incidents` unless `?status=merged`); severity `critical`, `high`, `medium`, `low` |
| `merged_into`, `merged_from` | the incident that took this one over (status `merged`), and the incidents this one took over |
| `detected_by`, `detector_rule` | `watcher` or `user`; the rule that fired first: `health_check`, `ecs_deployment_failed`, `ecs_tasks_stopped`, `log_error_spike`, `rds_cpu_latency`, `user_report`, `other` |
| `opened_at`, `resolved_at`, `window` | `window` = `{start, end}`; `end` is null while open |
| `affected_resource_ids`, `affected_resources` | ids for Map deep links; `{id, name, type, provider}` for labels |
| `root_cause_summary`, `triggering_change` | once diagnosed |
| `fixes_applied`, `pull_requests` | action names applied; PR refs with URL and state |
| `memory_reused` | a past incident's diagnosis or fix was reused |
| `prompt_tokens`, `completion_tokens`, `total_tokens`, `cost_usd`, `ultra_equiv_usd` | over all its Medic runs |
| `time_to_detect_ms`, `duration_ms` | first signal -> opened; opened -> resolved |
| `has_open_followups` | a follow-up (the permanent fix PR) is `open` or `merged` but not deployed |

**`IncidentDetail`** adds:

| Field | Meaning |
|---|---|
| `signals` | `[{rule, resource_id, resource_name, detail, at}]` |
| `evidence` | `[EvidenceItem {id, source, timestamp, resource_id, line}]`; claims cite these ids |
| `changes` | change markers for the timeline chart: `[{kind: deploy \| commit \| secret_change \| config \| other, ref, at, resource_id, label, url}]` |
| `root_cause` | `RootCause` (section 5), only cited claims |
| `fixes` | `[FixState {fix: ProposedFix, status, approval_id, pull_request, applied}]`; `fix.kind` is `pr` or `emergency`, `fix.plan_lines` is the plan as `[{what, before, after}]` lines, `pull_request` has the URL and state once opened; status `proposed`, `waiting_approval`, `rejected`, `applying`, `applied`, `failed` |
| `verify` | the last recovery check (shared `VerifyResult`) |
| `outcome` | `{result: resolved \| failed, reason, still_failing}` once final |
| `memory` | `{incident_id, title, similarity, reused: [diagnosis \| fix \| research]}` when a past incident was reused |
| `research` | Tavily results `[{title, url, snippet}]` (max 3) |
| `run_ids`, `error` | every Medic run for it, oldest first |
| `followups` | `[FollowUp {id, kind: fix_pr \| change_in_repo, state, repo, number, url, triggering_commit, merge_commit, merged_at, deployed_at, deploy_url, closed_at, dismissed_at, dismissed_by, created_at, updated_at, tracked_until}]` (below) |

**Follow-ups** (2026-10-14, `app/incidents/followups.py`, table `incident_followups`). An
emergency rollback can resolve an incident while its fix PR is still open; the default branch
then still has the bad change and the next deploy brings the outage back. When Medic resolves an
incident, each applied fix PR that is not deployed yet becomes a follow-up (`open`, or `merged`
if it merged already). The watcher job (same schedule) checks them through the GitHub App,
read-only, each at most every 2 minutes and for 7 days after creation:
- `open` -> `merged` (PR merged; `merge_commit`), then `merged` -> `deployed` when a successful
  workflow run whose path contains `deploy`, on the base branch, created after the merge,
  contains the merge commit (same SHA, or `compare` says ahead or identical). Done.
- `open` -> `closed_unmerged`: the service runs only on the rollback; `triggering_commit` is the
  change still in the repo.
- `change_in_repo` (no PR: `number`, `url` null): resolved by an emergency rollback with no fix
  PR open (rejected, closed, or never proposed) and a known triggering commit and repo. Red note
  "Running on a rollback; the repo still contains commit <sha>, so the next deploy will bring
  the failure back." `open` -> `reverted` when the newest successful deploy run on the default
  branch, created after the follow-up, no longer has the change: every file the commit touched
  (at most 30) has the same blob at the deploy's head as at the commit's parent. `open` ->
  `dismissed` by a person (`dismissed_by`). The installation comes from the workspace's GitHub
  connections; without one it is shown but not tracked. It does not hold memory back (the
  rollback was applied).
Each change records `incident_updated` on the incident's latest run (status unchanged,
`followups` set). Incident memory finds an incident only from `incidents.remembered_at`: at
resolve when no fix PR is open, else when the follow-up is merged or closed; a closed PR's fix is
left out of the reused fix ("rollback only"). `GET /workspaces/{id}/summary` has
`pending_permanent_fixes` (incidents with an open or merged follow-up). Not checked: the ECS
deployment of the new revision (the deploy workflow's success stands for it).

Stopping the Medic run (`POST /runs/{run_id}/stop`) rejects its pending approvals and closes the
incident as `failed` ("Stopped by <login>."). When Medic finds no fix, it waits
`MEDIC_NO_FIX_GRACE_SECONDS` (90) for related signals before failing: a signal from a resource or
rule the incident has not seen (the api's errors after the worker's) joins it and re-diagnoses.
`Signal.down`: an error spike where 90% or more of the service's log lines are errors for 2+
minutes (at least 3 lines) means "down" like a failing health check, so emergency fixes can be
proposed for services without health checks. Fix PR states are refreshed from GitHub when the
detail is read, at most once a minute.

Approve a fix with the existing `POST /approvals/{approval_id}/decide`; it also shows in the
approvals inbox (run agent `medic`).

## 7. Run stream events for incidents

Each event is a run step whose `kind` is the event name and whose `data` is typed
(`app/agents/medic/events.py`, `data.type` equals the event name), streamed under that name like
the deploy events. `GET /runs/{id}` returns the same steps.

| Event | `data` | Notes |
|---|---|---|
| `incident_opened` | `IncidentOpened` `{incident_id, title, severity, detected_by, detector_rule, signals, affected_resources, window}` | First step |
| `incident_updated` | `IncidentUpdated` `{incident_id, status, previous_status, at, reason, merged_into, followups}` | Every status change; also when follow-ups are created or change (`followups`: all of them, status unchanged) |
| `evidence_collected` | `EvidenceCollected` `{sources [{source, lines, truncated}], neighbors, items [EvidenceItem], changes [ChangeMarker], total_lines}` | |
| `memory_checked` | `MemoryChecked` `{hits [{incident_id, title, similarity, root_cause, fix_summary, resolved_at}], reused_incident_id, reused}` | |
| `research_done` | `ResearchDone` `{queries, results [{title, url, snippet}] (max 3), skipped_reason}` | Tavily |
| `diagnosis` | `Diagnosis` `{root_cause: RootCause, dropped}` | The `llm` step before it has model, tokens, cost |
| `fix_proposed` | `FixProposed` `{fixes [ProposedFix]}` | Ordered; emergency first when prod is down |
| `approval_requested` | `FixApproval` `{incident_id, fix: ProposedFix}`, plus `approval` | One per fix |
| `approval_decided` | | As for any run |
| `fix_applied` | `FixApplied` `{fix_id, kind, ok, action_result, pull_request, error}` | |
| `verify_result` | `VerifyResult` (the deploy payload) `{ok, checks [{name, ok, detail, ...}], duration_ms, logs_tail}` | One per re-check round |
| `incident_resolved` | `IncidentResolved` `{incident_id, time_to_detect_ms, duration_ms, fixes_applied, pull_requests, memory_reused, cost_usd, ultra_equiv_usd}` | Before `done` |
| `incident_failed` | `IncidentFailed` `{incident_id, reason, still_failing, cost_usd}` | Before `done` |

`ProposedFix`: `{fix_id, kind: pr | emergency, order, title, rationale, action, plan (ActionPlan),
plan_lines [{what, before, after}], pull_request {repo, base, branch, title, body, files [{path,
diff}]}, approval_id}`.

Verify checks: Medic uses `rollout` (ECS tasks running, deployment stable), `health`, `errors`
(log error rate), and `metric` (RDS CPU, latency). Rizwan adds `errors` and `metric` to
`VerifyCheck.name` in his M6; until then Medic sends `other` for those two.

## Per scenario (checkout-demo)

| Scenario | Rules that open it | Expected triggering change | Fixes, in order |
|---|---|---|---|
| bad-deploy-oom | `ecs_tasks_stopped` (exit 137, OutOfMemoryError), `ecs_deployment_failed` | commit to `worker/ecs-task.json` (deploy run) | `ecs_rollback_service` (worker), then PR restoring the memory |
| missing-index | `log_error_spike` / latency on `/orders/search`, `rds_cpu_latency` | commit adding a migration that drops `ix_orders_customer_email` | PR with a forward migration recreating the index concurrently |
| broken-env | `health_check`, `ecs_deployment_failed`, worker `poll failed` | secret `checkout/database-url` changed (no commit) | `secret_rollback_version`, then `force_redeploy` (api, worker); PR only if config is in the repo |

## Files in this PR

New: `app/actions/base.py`, `app/actions/fake.py`, `app/agents/medic/{events,schemas}.py`,
`app/telemetry/evidence.py`, `app/tools/github_pr.py`, `app/routes/incidents.py`,
`app/routes/incident_schemas.py`, `tests/test_phase5_contract.py`, `tests/medic_fakes.py`.
Changed (additive): `app/policy/actions.py` (action names), `app/routes/schemas.py` (`StepOut.data`
union, SSE names), `app/routes/runs.py` (incident events streamed by name), `app/main.py`
(router), generated `apps/web/lib/api/`, `docs/events.md`, `docs/api-changes.md`.

## Decisions

Agreed with Rizwan, 2026-10-12:

| # | Question | Answer |
|---|---|---|
| 1 | Operate tier | Scoped: `EnableOperate`, capabilities set, `groundcrew-operate` limited to `OperateServiceArns` / `OperateSecretArns` (section 2). Rizwan, target Oct 17; fakes until then |
| 2 | Plan then execute | Yes, approval bound to the plan hash, token consumed before the first write, `ok=False` when the state moved |
| 3 | Widen `ApprovedAction` | Yes, in this PR: additive, each action keeps its policy level, bound to plan hash and run |
| 4 | Resume dispatch | Rizwan adds handlers by `run.agent` in Phase 4 M6; Medic registers `"medic"` in M4 |
| 5 | GitHub App | Rizwan owns it; Contents and Pull requests are already read and write; branch protection on checkout-demo `main` |
| 6 | `VerifyCheck.name` | Rizwan adds `errors` and `metric` in M6 |
| 7 | Incidents UI | Filters, structured evidence with ids, claims citing ids, change markers, plan lines, PR state, outcome, memory reuse, `incident_updated`: all in sections 5 to 7 |
