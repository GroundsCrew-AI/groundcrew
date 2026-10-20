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
- `POST /incidents` takes `test: true` for test or demo reports, and `IncidentSummary.test` /
  `IncidentDetail.test` say so. Medic runs as usual, but a test incident gets no embedding, is
  never returned by incident memory, and is never deduped or merged with real incidents (or real
  ones with it). The Report dialog has a "This is a test" checkbox; the list shows a "Test" tag.
  Migration `a4f2c7e9b1d6` adds `incidents.is_test`.

## Phase 5: emergency fix retry (2026-10-14)

All additive.
- `POST /incidents/{incident_id}/fixes/{fix_id}/retry` -> `IncidentDetail`. Only for a fix with
  `kind=emergency` and `status=failed` on an unresolved incident, while Medic is done or
  verifying (409 otherwise; 404 for an unknown fix). Plans the action again from the current
  state; 422 with the reason when it cannot be planned (e.g. "rollback needs PassRole on ...").
  On success the fix (and the fixes it held back via `depends_on`) are `waiting_approval` with
  new approvals (`tool_call_id` `medic:<incident>:<fix>:retry-<n>`) on the same Medic run, which
  goes back to `waiting_approval`; the incident too. Approving resumes the run as usual.
- Research results are de-duplicated by URL.

## Phase 5: incident follow-ups (2026-10-14)

All additive. Migration `b7d1e3f5a9c2` (single head) adds `incident_followups` and
`incidents.remembered_at`, and backfills follow-ups for resolved incidents whose stored fix PR
was open or merged.
- `IncidentDetail.followups: FollowUp[]` and `IncidentSummary.has_open_followups` (also on the
  detail). `FollowUp` = `{id, kind: "fix_pr", state: open | merged | closed_unmerged |
  deployed, repo, number, url, triggering_commit, merge_commit, merged_at, deployed_at,
  deploy_url, closed_at, created_at, updated_at, tracked_until}`.
- `GET /incidents?pending_fix=true|false`.
- `IncidentUpdated.followups` (null unless follow-ups were created or changed; the status may
  stay `resolved`).
- `WorkspaceSummary.pending_permanent_fixes`.

## Phase 5: live broken-env fixes (2026-10-14)

All additive.
- `IncidentStatus` `merged`: a newer incident on a related resource took this one over (was
  `failed`). `GET /incidents` leaves merged incidents out unless `status=merged` (listed by
  default since 2026-10-19).
  `IncidentSummary.merged_into` (uuid or null) and `merged_from` (uuids). `incident_updated`
  on the merged incident has `status: "merged"`, `previous_status`, `merged_into`.
- `Signal.down` (bool): an error spike where nearly every operation fails counts as down.
- `incident_updated` "No fix yet; waiting 90 s for related signals" before "No fix".
- Secret version ids are shown in full (plan summary, the Map's `version_stages` keys).

- `FollowUp.kind` `change_in_repo` (rolled back, no fix PR on its way): states `open`,
  `reverted`, `dismissed`; `number` and `url` are null. New `FollowUp.dismissed_at` and
  `dismissed_by`. `POST /incidents/{incident_id}/followups/{followup_id}/dismiss` ->
  `IncidentDetail` (409 unless an open `change_in_repo`).
  Migration `c3e8a1f4b6d7` (after `b7d1e3f5a9c2`): number, url, installation_id nullable;
  dismissed_at, dismissed_by added.

## Phase 5: live broken-env follow-ups (2026-10-14)

All additive.
- `IncidentSummary.first_signal_at`: the onset of the signal that opened the incident (not the
  earliest signal of an incident it took over). The timeline's "First signal" mark uses it.
- A re-diagnosis that proposes the same fixes (same plans or PR drafts, same order) keeps them,
  their fix ids, and their approvals; a `fix_proposed` step says "Same fixes as before the new
  signals; their approvals stay". A decision made during the re-diagnosis goes on. Different
  fixes close the old approvals (`decided_via` "Medic: superseded by new signals") as before.
- Change markers: changes to tooling resources (`groundcrew:role=tooling`, name rules) are left
  out unless the incident names the resource; Vercel deployments whose `meta` has
  `groundcrew:role` or `groundcrew_role` = `tooling` are skipped; Vercel redeploys are labeled
  "<project> redeploy READY (same source, commit <sha>)". The UI folds back-to-back repeats into
  one row with a count.

## Phase 5: degraded emergency fixes, merged incidents in the list (2026-10-19)

All additive, except that `GET /incidents` now lists merged incidents by default.
- `ProposedFix.degraded` (string or null): set on an emergency fix offered while the service is
  degraded, not down. It says why the fix is offered anyway ("It reverts the change that
  started the errors." or "The same fix resolved <incident> (95% similar)."). Null when the
  service is down. The approval card shows "Service degraded, not down".
- `GET /incidents` without `status` now includes `merged` incidents.
  `IncidentSummary.merged_into_title`: the title of the incident that took this one over.

## Phase 5: Kubernetes incidents (2026-10-19)

All additive.
- `DetectorRule`: `k8s_rollout_failed` (a Deployment passed its progress deadline) and
  `k8s_pods_crashing` (CrashLoopBackOff, image pull or create errors, OOMKilled or non-zero
  exits). `health_check` also covers pods failing their readiness probe; `log_error_spike`
  also reads pod logs. A signal is `down` when no pod of the Deployment is ready.
- `EmergencyActionName` / `ProposedFix.action`: `k8s_rollout_undo`, `k8s_rollout_restart`;
  `FixOptionKind` gains the same two.
- `POST /connections/{connection_id}/k8s/operate` `{enabled}` (owner): turns approved emergency
  fixes on for a cluster after access reviews show the kubeconfig may patch Deployments and list
  ReplicaSets (`infra/k8s-operate-rbac.yaml`); 422 with what to apply otherwise.
  `ConnectionOut.capabilities` is now set for Kubernetes connections too (`observe`, `operate`).
- `infra/k8s-readonly-rbac.yaml` adds `events`, `replicasets`, and `pods/log` (get): re-apply it
  on clusters connected before.

## Per-connection AWS regions (2026-10-19)

All additive; connections without a choice behave as before.
- `PUT /connections/{connection_id}/aws/regions` `{regions: [...]}` (owner): the regions
  Groundcrew discovers, watches, and acts in for that account (at most 10; AWS region names,
  lower-cased and de-duplicated). `[]` goes back to the platform default DISCOVERY_REGIONS.
- `ConnectionOut.regions` (AWS: the effective list) and `regions_chosen` (the owner chose them).
- Emergency actions run in the region of the resource's ARN, which must be one of the
  connection's regions.
- The read-only role is global, so no stack update is needed. The Resource Explorer index exists
  only in the stack's region; elsewhere discovery reads ECS, RDS, Lambda, and the rest directly.

## Lambda and EC2 incidents (2026-10-19)

All additive.
- `DetectorRule`: `lambda_errors` (5+ errors in 5 minutes, 5 %+ of invocations, 3x the rate
  before; down at 90 %), `lambda_throttles`, `ec2_status_check` (a system or instance status
  check impaired; down). The function's log group feeds `log_error_spike` too.
- `EmergencyActionName` / `FixOptionKind`: `lambda_rollback_alias`, `ec2_reboot_instance`.
- Connect template: the read-only role adds `lambda:ListVersionsByFunction`,
  `lambda:ListAliases`, `lambda:GetAlias`, `ec2:DescribeInstances`,
  `ec2:DescribeInstanceStatus`. EnableOperate adds `OperateFunctionArns` (GetAlias,
  ListVersionsByFunction, UpdateAlias) and `OperateInstanceArns` (RebootInstances); the rule now
  needs real ARNs in at least one Operate* list instead of both services and secrets. Existing
  stacks keep working; update them with the new template to get these.

## Kubernetes deploys to real clusters (2026-10-19)

All additive.
- `PUT /connections/{connection_id}/k8s/deploy-settings` (owner, connected k8s only):
  `K8sDeploySettingsIn` `{registry, username?, token?, pull_secret, ingress_class, app_domain?,
  tls, tls_cluster_issuer?, public_url_base?}`. `registry` is `host[:port][/path]` without a
  scheme; `app_domain` and `ingress_class` are DNS names; private or loopback registries, app
  domains, and `public_url_base` hosts are refused unless `K8S_ALLOW_PRIVATE_SERVERS` (default
  on in `APP_ENV=dev`). `token: null` keeps the saved token for the same username; no username
  clears the credentials; `pull_secret` and a token need a username; `tls` needs `app_domain`;
  `tls_cluster_issuer` needs `tls`. 422 with the reason otherwise. Returns `ConnectionOut`.
- `DELETE /connections/{connection_id}/k8s/deploy-settings` (owner): forget them (back to the
  local kind defaults). Returns `ConnectionOut`.
- `ConnectionOut.deploy_settings` (`K8sDeploySettingsOut` or null): the settings without the
  token; `token_saved` says whether one is stored. Null for kind defaults and other kinds.
- Deploy plans to such a cluster: `DeployPlan.image` is `<registry>/<app>:<tag>`, an extra
  `Secret <app>-registry` step (imagePullSecret, value masked), and `expected_url` is
  `https://<app>.<app_domain>` with TLS. Preflight has a new `other` check: it fails on a cluster
  that is not the local kind cluster and has no deploy settings.

## Kubernetes StatefulSets, ConfigMap and Secret changes (2026-10-19)

All additive.
- New `PUT /connections/{id}/k8s/configmap-changes` (owner only), body `{enabled: bool}`:
  opt in to ConfigMap changes as incident causes. Off by default, because the `list configmaps`
  right it needs also grants read access to ConfigMap contents. On: a SelfSubjectAccessReview
  checks the kubeconfig may list ConfigMaps (cluster-wide or in a discovered workload's
  namespace), else 422 and nothing changes. `ConnectionOut.configmap_changes` (bool, null for
  non-k8s) carries the choice. Off: a workload that uses ConfigMaps gets the evidence line
  "ConfigMap changes not visible (opt-in): ..." instead of a silent miss.
- Incidents now cover `k8s:statefulset` resources like `k8s:deployment`: `k8s_pods_crashing`,
  `health_check`, `log_error_spike`, and `k8s_rollout_failed` for a stuck rollout
  (currentRevision is not updateRevision, pods failing, 5+ minutes after the new revision).
  Evidence sources for them are keyed `k8s:statefulset/<name>`.
- Change markers (`ChangeMarker`): a StatefulSet's ControllerRevision created in the window is a
  `deploy` marker, ref `<ns>/<name> revision <n>`. A ConfigMap or Secret a workload's pods use
  that changed in the window is a `config` marker (ref `<ns>/configmap/<name>@<resourceVersion>`,
  only with the opt-in above) or a `secret_change` marker (ref `<ns>/secret/<name>@<resourceVersion>`), label
  "ConfigMap shop/web-config changed". Read as metadata only; no value ever reaches Groundcrew.
- `ActionPlan.params` for `k8s_rollout_undo` / `k8s_rollout_restart` on a StatefulSet:
  `{namespace, statefulset, kind: "statefulset"}`. A Deployment's stay `{namespace, deployment}`.
- A ConfigMap or Secret change as the cause gets no emergency fix, only a note in the run
  ("ConfigMap shop/web-config changed at <time>; restore it, then Groundcrew can restart web.").
- `POST /connections/{id}/k8s/operate` also accepts a kubeconfig that may patch StatefulSets and
  list ControllerRevisions (and counts discovered StatefulSets' namespaces); the 422 text now
  says "may not patch Deployments or StatefulSets".
- RBAC: infra/k8s-readonly-rbac.yaml adds `controllerrevisions`, plus optional, commented-out
  `list configmaps` and `list secrets` blocks (metadata only; each grants read access to
  contents, so they are opt-in); infra/k8s-operate-rbac.yaml adds
  `patch statefulsets` and `get/list controllerrevisions`. Re-apply them to get these.
