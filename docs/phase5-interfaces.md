# Phase 5 interfaces (Medic)

The contract between Medic (Ahmed: telemetry, incidents, `app/agents/medic/`, the incidents API)
and Rizwan (Incidents UI, Operate-tier role, `app/actions/`). Both sides build against this file
and the code it names. M0 lands the types, the 501 routes, and fakes for both sides; nothing here
talks to AWS or GitHub yet.

Status: **proposed** (M0). Items marked **Ask** need Rizwan's OK before they are final.

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
(`FakeEmergencyActions`), which Medic uses until the real one lands.

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
2. Re-read the state; if it no longer equals `plan.current`, return `ok=False` ("the state changed
   since the plan was approved") and write nothing.
3. Return `ActionResult(ok, summary, details={...ids...}, error)`; do not wait for the service to
   become stable (Medic's verify does that).
4. AWS only through `CredentialsProvider.aws_operate_session(connection_id)`.

`ActionPlan.plan_hash()` covers action, connection, target, params, current, desired (not the
wording or the time). `EmergencyActionError` (safe message) when no plan is possible: no previous
revision, no `AWSPREVIOUS`, connection not Operate tier.

**Ask (Operate tier):** `CredentialsProvider.aws_operate_session(connection_id)`, a `tier=operate`
connection setting, and a `groundcrew-operate` role allowed exactly: `ecs:DescribeServices`,
`ecs:ListTaskDefinitions`, `ecs:DescribeTaskDefinition`, `ecs:UpdateService`,
`secretsmanager:DescribeSecret`, `secretsmanager:UpdateSecretVersionStage`, with the same explicit
Deny on secret values as the other roles, and guarded at runtime by an `AWS_OPERATE_OPERATIONS`
allowlist. How it is scoped (every service and secret in the account, or ones named or tagged in
the stack) is Rizwan's call; the demo needs `service/checkout/*` and `secret:checkout/*`.

## 3. Fix pull requests (Ahmed, `app/tools/github_pr.py`)

```python
class FixPullRequests(Protocol):
    async def open_fix_pr(self, draft: PullRequestDraft, token: ApprovedAction, *, run_id) -> PullRequestRef
```

`PullRequestDraft` holds `installation_id, repo, base, branch, title, body, files: dict[path,
content]` (the prompt's `open_fix_pr(installation_id, repo, branch, files, title, body)` as one
approvable object). It always opens a new `groundcrew/fix-*` branch from `base` and a PR: never a
commit to the default branch, never a merge. The approval binds `draft.plan_hash()` (file
contents included). `FakeFixPullRequests` records PRs with the real approval check. The GitHub
calls land in M3 in Medic's own module, with a write allowlist of its own (refs under
`groundcrew/fix-*`, git blobs/trees/commits, pulls); `app/connectors/github.py` stays GET only.

**Ask (GitHub App):** the App needs **Contents: write** and **Pull requests: write** on the
checkout-demo installation (it has read today). Who changes the App settings, and does the
installation need re-approving?

## 4. Approvals and tokens (`app/policy/actions.py`, small change in this PR)

Medic creates one approval per fix in its run (`tool_call_id = "medic:<incident>:<fix_id>"`,
`arguments = {"plan_hash": ..., "fix_id": ...}`) and, after approval, gets a token with
`issue_approved_action(...)`. This PR widens the token's action names (no behaviour change for
targets):

| Tool name | Policy | `plan_hash` of |
|---|---|---|
| `open_fix_pr` | write | `PullRequestDraft` |
| `ecs_rollback_service` | emergency | `ActionPlan` |
| `secret_rollback_version` | emergency | `ActionPlan` |
| `force_redeploy` | emergency | `ActionPlan` |

**Ask:** OK to widen `ApprovedAction.action` (`TargetAction | MedicAction`) in `app/policy/actions.py`?

**Ask (resume):** deciding an approval enqueues `resume_run`, which today always goes to
`AgentRunner` (the tool loop). Deploys (M6) and Medic both need their own resume. Proposal:
`resume_run` looks up a handler by `run.agent` (`{"deploy": ..., "medic": ...}`, default
`AgentRunner`) in `app/agents/base.py` / `app/worker.py`. Who adds it, so M6 and M4 do not collide?

## 5. RootCause (Ahmed, `app/agents/medic/schemas.py`)

Structured output of the `root_cause` task (Ultra/on, Super with `DEV_MODE_NO_ULTRA=true`):

```python
class RootCause(BaseModel):
    summary: str
    root_cause: str
    triggering_change: TriggeringChange | None   # {kind: commit|secret_change|deploy|config|other, ref, at, description}
    affected_resources: list[str]
    evidence: list[EvidenceRef]                  # {source, timestamp, line}
    confidence: Literal["high", "medium", "low", "other"]
    fix_options: list[FixOption]                 # {kind: pull_request|ecs_rollback_service|secret_rollback_version|force_redeploy|other, description, target}
```

After the call Medic drops every `evidence` entry whose line is not in the collected evidence, and
any claim left with nothing cited (listed in `Diagnosis.dropped`). `fix_options` are suggestions:
Medic builds the concrete PR draft or `ActionPlan` itself and never runs model text as an action.

## 6. Incidents API (Ahmed; Rizwan's UI)

Code: `app/routes/incidents.py` (501 until M4, behind auth and membership like `/deploys`),
`app/routes/incident_schemas.py`. Types are in `apps/web/lib/api/schema.d.ts`.

| Method | Path | Body / query | Response |
|---|---|---|---|
| POST | `/incidents` | `IncidentCreate` `{workspace_id, resource_ids (1..20), title?, description?, since?}` | 202 `IncidentCreated` `{incident_id, run_id, deduped}` |
| GET | `/incidents` | `workspace_id`, `status?`, `limit` (1..100, 25), `cursor` | 200 `IncidentPage`, newest first |
| GET | `/incidents/{incident_id}` | | 200 `IncidentDetail` |

- Dedupe: one open incident per group of related resources. Reporting resources an open
  incident covers returns that incident with `deduped: true`.
- `IncidentSummary`: `id, workspace_id, title, status, severity, detected_by (watcher|user),
  affected_resources [{id, name, type, provider}], run_id, window_start, window_end,
  root_cause_summary, triggering_change, fixes_applied, pull_requests [{repo, number, url, branch,
  head_sha}], memory_reused, cost_usd, ultra_equiv_usd, created_at, resolved_at,
  time_to_detect_ms, duration_ms`.
- `IncidentDetail` adds `signals, root_cause (RootCause), evidence (top lines), similar_incidents,
  research [{title, url, snippet}], fixes [{fix: ProposedFix, status, applied}], verify
  (VerifyResult), run_ids, error`.
- Status: `open`, `investigating`, `waiting_approval`, `applying`, `verifying`, `resolved`, `failed`.
  Severity: `critical`, `high`, `medium`, `low`.
- Approve a fix with the existing `POST /approvals/{approval_id}/decide`; it also shows in the
  approvals inbox (run agent `medic`).

## 7. Run stream events for incidents

Each event is a run step whose `kind` is the event name and whose `data` is typed
(`app/agents/medic/events.py`, `data.type` equals the event name), streamed under that name like
the deploy events. `GET /runs/{id}` returns the same steps.

| Event | `data` | Notes |
|---|---|---|
| `incident_opened` | `IncidentOpened` `{incident_id, title, severity, detected_by, signals, affected_resources, window_start, window_end}` | First step |
| `evidence_collected` | `EvidenceCollected` `{sources [{source, lines, truncated}], neighbors, top [EvidenceRef], total_lines}` | |
| `memory_checked` | `MemoryChecked` `{hits [{incident_id, title, similarity, root_cause, fix_summary, resolved_at}], reused}` | Added to the prompt's list: the memory hit is a demo moment |
| `research_done` | `ResearchDone` `{queries, results [{title, url, snippet}] (max 3), skipped_reason}` | Tavily |
| `diagnosis` | `Diagnosis` `{root_cause: RootCause, dropped}` | The `llm` step before it has model, tokens, cost |
| `fix_proposed` | `FixProposed` `{fixes [ProposedFix]}` | Ordered; emergency action first when prod is down |
| `approval_requested` | `FixApproval` `{incident_id, fix: ProposedFix}`, plus `approval` | One per fix |
| `approval_decided` | | As for any run |
| `fix_applied` | `FixApplied` `{fix_id, kind, ok, action_result, pull_request, error}` | |
| `verify_result` | `VerifyResult` (the deploy payload) `{ok, checks [{name, ok, detail, ...}], duration_ms, logs_tail}` | One per re-check round |
| `incident_resolved` | `IncidentResolved` `{incident_id, time_to_detect_ms, duration_ms, fixes_applied, pull_requests, memory_reused, cost_usd, ultra_equiv_usd}` | Before `done` |
| `incident_failed` | `IncidentFailed` `{incident_id, reason, still_failing, cost_usd}` | Before `done` |

`ProposedFix`: `{fix_id, kind: pull_request|emergency_action, order, title, rationale, action,
plan (ActionPlan), pull_request {repo, base, branch, title, body, files [{path, diff}]},
approval_id}`.

**Ask (verify checks):** Medic reuses the deploy `VerifyResult` so the UI can share one component.
Its `VerifyCheck.name` is `rollout | health | root | other`; Medic would use `rollout` (ECS tasks
running and the deployment stable), `health`, and `other` for error rate and RDS CPU/latency.
OK to add `errors` and `metric` to that Literal in `app/pipeline/events.py` (additive)?

## Per scenario (checkout-demo)

| Scenario | Signals that open it | Expected triggering change | Fixes, in order |
|---|---|---|---|
| bad-deploy-oom | worker tasks stopped exit 137 (OutOfMemoryError), deployment failed | commit to `worker/ecs-task.json` (deploy run) | `ecs_rollback_service` (worker), then PR restoring the memory |
| missing-index | `/orders/search` latency up, RDS CPU spike | commit adding a migration that drops `ix_orders_customer_email` | PR with a forward migration recreating the index concurrently |
| broken-env | api health check failures, replacement loop, worker `poll failed` | secret `checkout/database-url` changed (no commit) | `secret_rollback_version`, then `force_redeploy` (api, worker); PR only if config is in the repo |

## Files in this PR

New: `app/actions/base.py`, `app/actions/fake.py`, `app/agents/medic/{events,schemas}.py`,
`app/telemetry/evidence.py`, `app/tools/github_pr.py`, `app/routes/incidents.py`,
`app/routes/incident_schemas.py`, `tests/test_phase5_contract.py`, `tests/medic_fakes.py`.
Changed (additive): `app/policy/actions.py` (action names), `app/routes/schemas.py` (`StepOut.data`
union, SSE names), `app/routes/runs.py` (incident events streamed by name), `app/main.py`
(router), generated `apps/web/lib/api/`, `docs/events.md`, `docs/api-changes.md`.

## Questions for Rizwan (one place)

1. Operate tier: role name, scope, `aws_operate_session`, and when it can land (section 2).
2. Plan + execute split for emergency actions, with the state re-check (section 2): OK?
3. Widening `ApprovedAction.action` in `app/policy/actions.py` (section 4): OK?
4. `resume_run` dispatch by `run.agent`: who adds it (section 4)?
5. GitHub App write permissions for fix PRs (section 3).
6. `VerifyCheck.name`: add `errors` and `metric` (section 7)?
7. Incidents UI on the run stream with the events above: anything missing for the screens?
