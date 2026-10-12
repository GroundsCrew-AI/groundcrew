# Deploy API contract

`POST /deploys` takes a repository from GitHub to a live URL on a Kubernetes cluster or AWS ECS in
one run: Scout reads the repo, Builder writes and fixes a Dockerfile in the sandbox, Deployer plans,
asks for approval, applies, and verifies. This file is the contract between the API, the web app,
and Medic (Phase 5). The live events are in [events.md](events.md).

Status: live (Phase 4, M6). A deploy is a run with `agent = "deploy"`; its `deploy_id` is the
run's id.

Code: `apps/api/app/routes/deploys.py` and `deploy_schemas.py` (requests and responses),
`apps/api/app/pipeline/runner.py` (the pipeline), `apps/api/app/pipeline/events.py` (event
payloads), `apps/api/app/pipeline/state.py` (`runs.state`).

## Routes

Every route needs the signed-in user (internal JWT from the web proxy). A workspace, deploy, or
connection the user is not a member of is a 404, like everywhere else.

| Method | Path | Body / query | Response |
|---|---|---|---|
| POST | `/deploys` | `DeployCreate` | 202 `DeployCreated` |
| GET | `/deploys` | `workspace_id`, `limit` (1 to 100, default 25), `cursor` | 200 `DeployPage`, newest first |
| GET | `/deploys/{deploy_id}` | | 200 `DeployDetail` |
| POST | `/deploys/{deploy_id}/env` | `DeployEnvUpdate` | 202 `DeployEnvUpdated` (below) |

Follow a deploy live with the run stream: `POST /runs/{run_id}/stream-token`, then
`GET /runs/{run_id}/events?token=`. Stop it with `POST /runs/{run_id}/stop`. Approve its plan with
`POST /approvals/{id}/decide`. A deploy is a run with `agent = "deploy"`, so it also shows in the
runs list and the approvals inbox.

## POST /deploys

```json
{
  "workspace_id": "6f0c...",
  "repo": { "connection_id": "1b2e...", "repo": "GroundsCrew-AI/legacy-flask-app", "ref": "main" },
  "target_connection_id": "9a7d...",
  "env": [{ "name": "DATABASE_URL", "value": "postgresql://...", "secret": true }],
  "options": { "allow_missing_env": true }
}
```

| Field | Rules |
|---|---|
| `repo` | Either `{connection_id, repo, ref}` (a GitHub connection in the workspace; `repo` is `owner/name`; `ref` is a branch, tag, or SHA) or `{fixture}` (a folder under `demo/fixtures/`, dev only). Mixing the two shapes is a 422. |
| `target_connection_id` | A `k8s` connection, or an `aws` connection with `Tier=deploy`. An observe-tier AWS connection is refused with 409 and a "Turn on deploys" hint. |
| `env` | Up to 100 vars, unique names matching `[A-Za-z_][A-Za-z0-9_]*`, values up to 16,384 characters. `secret: true` sends the value to a Kubernetes Secret or Secrets Manager. |
| `options.allow_missing_env` | Deploy even when Scout marked a var required and none was given. Default false: preflight fails and the plan cannot be approved. |

Unknown fields anywhere are a 422. Response: `{"deploy_id": "...", "run_id": "..."}`.

### Secrets

- Every env value is a `SecretStr` on the way in and is never returned: responses list names,
  whether each is secret, and whether a value was given (`EnvVarView`).
- A 422 never echoes an env value (`app/main.py` drops `input` for errors under `body.env`).
- Values live Fernet-encrypted in the job payload only. They are never written to `runs.state`,
  approvals, run steps, or logs, and the job copy is purged when the run ends.
- The approval card shows the rendered plan with every secret value replaced by `********`.
  Plain (non-secret) values do appear in the plan, because the plan is what gets approved.

## Responses

`DeploySummary` (one row of `GET /deploys`):

| Field | Meaning |
|---|---|
| `id`, `run_id`, `workspace_id` | |
| `status` | `queued`, `running`, `waiting_approval`, `live`, `failed`, `rolled_back`, `stopped` |
| `run_status`, `stop_reason` | The run's own status and stop reason (`no_progress` when Builder repeated the same fix) |
| `stage` | The stage running now, or the last one reached (see below) |
| `app` | App name from Scout, once known |
| `source` | `{kind: github \| fixture, repo, ref, connection_id, commit}`; `commit` once checked out |
| `target` | `{connection_id, kind: k8s \| aws_ecs, name}` |
| `live_url` | Once verified |
| `build_attempts`, `automatic_fix` | Attempts used, and whether a build passed after Builder changed the Dockerfile because of a failure |
| `cost_usd`, `ultra_equiv_usd` | Token Factory cost of the run, and the Ultra-only equivalent |
| `error` | Why it failed or stopped |
| `created_by`, `created_at`, `started_at`, `finished_at`, `duration_ms` | |

`DeployDetail` adds `env` (names and flags only), `allow_missing_env`, `stages` (one entry per stage
reached, with status and times), `plan` (the `DeployPlan` from the approval card), and `deploy` (the
final `DeployRecord`, below).

## Stages

In order: `scout`, `build`, `smoke` (Builder), `plan`, `preflight`, `approval`, `apply`, `verify`
(Deployer), and `rollback` only after a failed verify. Each emits `stage_started` and
`stage_finished` (`succeeded`, `failed`, or `skipped`). A stage can run again: `preflight` and
`approval` repeat when the env changes at the approval card (below).

**Nothing leaves the sandbox before approval.** Before it, the image is only built and
smoke-tested in the sandbox; preflight reports "Built and smoke-tested (sha256:...)". Pushing
it is the first step of `apply`: on ECS, create the ECR repository, then push; on Kubernetes,
push to the cluster registry (`kind-registry:5000`). There is no `push` stage. The plan lists
the push as its first apply step (`resources`, action `push`), and `source_image` pins the exact
image that approving pushes.

- **build**: at most 5 attempts. A repo Dockerfile is built as is first. Attempt 4 escalates
  `fix_build` one tier (Super while `DEV_MODE_NO_ULTRA=true`). The same error after the same kind
  of fix stops the run with `stop_reason = no_progress`.
- **approval**: nothing touches infrastructure, and no image is pushed, before a workspace
  member approves the exact rendered plan. The approval's arguments are the plan; approving
  runs it once.
- **apply**: runs `resources` in order, starting with the image push.
- **verify** fails: Deployer collects logs and offers a rollback as an emergency approval
  (`rollback_offered`). Rejecting it leaves the failed deployment in place.

## Fixing missing env at approval

When preflight fails only because a required env var is missing, the approval card offers
**Set value** (a masked input) or **Continue without it**. Either one re-runs only `preflight`
and re-renders the plan with the image already built and smoke-tested: no rebuild, no new
smoke test. The approval card calls:

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/deploys/{deploy_id}/env` | `DeployEnvUpdate` | 202 `{"approval_id": "<the replaced approval>"}` |

```json
{
  "approval_id": "c41e...",
  "env": [{ "name": "DATABASE_URL", "value": "postgresql://...", "secret": true }],
  "allow_missing_env": false
}
```

- `approval_id` must be the deploy's pending plan approval (409 otherwise). It is closed as
  `rejected` by the caller, `approval` finishes as `skipped` ("Plan replaced: DATABASE_URL set"),
  then `preflight` runs again and a new `approval_requested` carries the new plan. The web app
  shows the replaced plan as a short note.
- `env` uses the `EnvVarIn` rules and is merged by name into the deploy's env; values are
  handled exactly like `POST /deploys` (SecretStr, Fernet in the job payload, never echoed).
- `allow_missing_env: true` is "Continue without it": the env check stays `fail` with
  `overridden: true`, and the plan can be approved.

## `runs.state` (for Medic)

`runs.state` is JSONB on every run (empty for other agents). For `agent = "deploy"` it is
`PipelineState`: `deploy_id`, `stages` (status, times, pending `approval_id`, so a paused run
resumes in a new job), and `deploy`, written once when the run reaches a final deploy status:

```python
from app.pipeline.state import PipelineState
record = PipelineState.model_validate(run.state).deploy  # DeployRecord | None
```

| `DeployRecord` field | Example |
|---|---|
| `target` | `k8s` or `aws_ecs` |
| `connection_id` | target connection |
| `app` | `legacy-flask-app` |
| `image` | `kind-registry:5000/legacy-flask-app:3f2a9c1b7d4e`, or the ECR URI (pushed during apply) |
| `commit` | commit SHA that was built (null for fixtures) |
| `repo` | `GroundsCrew-AI/legacy-flask-app`, or `fixture:<name>` |
| `plan_summary` | `Deployment gc-legacy-flask-app/legacy-flask-app, 1 replica, 250m / 512Mi` |
| `live_url` | `http://legacy-flask-app.localhost` or `http://<task IP>:8000` |
| `revision` | Deployment revision (k8s) or task definition ARN (ECS) |
| `previous_revision` | what `rollback()` returns to; null on a first deploy |
| `started_at`, `finished_at` | |
| `status` | `live`, `failed`, or `rolled_back` |

On success the deployment is also registered once with `ResourceSink.register_deployment`, so it
appears on the Map with a `deployed_by` edge to Groundcrew. Unknown keys in `runs.state` are kept,
so later fields do not break older readers.

## Errors

| Status | When |
|---|---|
| 401 | Not signed in |
| 404 | Workspace, deploy, or connection not found for this user |
| 409 | Target is an AWS connection without `Tier=deploy` (or not a deploy target), the source connection is not GitHub, a fixture source outside `APP_ENV=dev`; for `/env`, the approval is not the deploy's pending plan |
| 422 | Body does not match the schema (env values never echoed) |

## Dev-only self-approve (live test scripts)

`POST /approvals/{approval_id}/self-approve` with `{"script": "<name>"}` approves a pending
approval as `script:<name> (dev)` (stored in `approvals.decided_via`, shown in the inbox and
the timeline). It answers 404 unless `APP_ENV=dev` and `LIVE_SCRIPT_SELF_APPROVE=1`, only for
members of the run's workspace, and it never rejects. `scripts/live_phase4.sh` uses it; a
person approving in the UI never needs it.
