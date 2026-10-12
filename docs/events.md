# Run stream events

`GET /runs/{run_id}/events?token=` is a Server-Sent Events stream. Get the 60-second token from
`POST /runs/{run_id}/stream-token`. The stream sends what the run already has, then new events
until the run ends. Reconnect with `Last-Event-ID` (or `?after=<seq>`) to continue after the last
step you saw. A `: heartbeat` comment every 15 s keeps proxies from closing it.

Every payload type is in the OpenAPI schema (`apps/web/lib/api/schema.d.ts`). The models live in
`apps/api/app/routes/schemas.py` and, for deploys, `apps/api/app/pipeline/events.py`.

## All runs

| Event | `id` | Payload | When |
|---|---|---|---|
| `step` | step seq | `StepOut` | Any step that is not one of the events below |
| `approval_requested` | step seq | `ApprovalStepEvent` `{step, approval}` | A write or emergency action waits for a person |
| `approval_decided` | step seq | `ApprovalStepEvent` | Someone approved or rejected it |
| `status` | | `RunStatusEvent` | The run's status, stop reason, or totals changed |
| `done` | | `RunStatusEvent` | Last event; the stream closes |

## Deploy runs (`agent = "deploy"`)

Each deploy event is a run step whose `kind` is the event name. The payload is the `StepOut`, with
the typed details in `step.data`, discriminated by `data.type` (always equal to the event name).
`GET /runs/{id}` returns the same steps, so a page reload rebuilds the same timeline, and resuming
with `Last-Event-ID` works for these events too.

| Event | `data` | Notes |
|---|---|---|
| `stage_started` | `StageStarted` `{stage, agent}` | `agent`: `scout`, `builder`, or `deployer` |
| `stage_finished` | `StageFinished` `{stage, agent, status, duration_ms, summary}` | `status`: `succeeded`, `failed`, `skipped` |
| `build_attempt` | `BuildAttempt` | One per Docker build, at most 5 |
| `approval_requested` | `DeployPlan` in `step.data`, plus `approval` | The plan to approve; also for a rollback |
| `approval_decided` | | As for any run |
| `verify_result` | `VerifyResult` `{ok, checks, duration_ms, logs_tail}` | `checks`: `rollout`, `health`, `root` |
| `live_url` | `LiveUrl` `{url, target, app, resource_id}` | `resource_id` is the Map resource ("View on Map") |
| `rollback_offered` | `RollbackOffered` `{approval_id, reason, from_revision, to_revision}` | Followed by its `approval_requested` |
| `run_finished` | `RunFinished` | Sent before `done` |

Model calls (Scout's `detect_app_spec`, Builder's `write_dockerfile` / `fix_build`, Deployer's
`plan_deploy`) stay ordinary `step` events with `kind = "llm"`, carrying the model badge, tokens,
and cost.

### `BuildAttempt`

| Field | Meaning |
|---|---|
| `attempt`, `max_attempts` | 1 to 5 |
| `dockerfile_source` | `repo` (built as is), `generated` (`write_dockerfile`), `fixed` (`fix_build` after a failure) |
| `dockerfile_diff` | Unified diff against the previous attempt; the whole file on attempt 1 |
| `rationale` | The model's one-paragraph reason for the change |
| `ok`, `duration_ms` | |
| `failing_step`, `error_summary` | On failure: the Dockerfile instruction and a one-line cause |
| `log_tail` | Last 8,000 characters of the build log, redacted |
| `image_ref` | On success: the image in the sandbox. It is pushed only after approval, as the first step of `apply` |
| `model_call` | The call that wrote this Dockerfile: `{task, model_tier, model_id, reasoning_mode, escalated, prompt_tokens, completion_tokens, cost_usd, step_seq}`; null for `repo` |

### `DeployPlan` (on `approval_requested`)

`target`, `target_connection_id`, `app`, `image` (where apply pushes it), `source_image` (the
sandbox image ID that was built and smoke-tested, which approving pushes; null on a rollback),
`port`, `health_path`, `replicas`, `cpu`, `memory`, `env` (`EnvVarView`: `name`, `secret`,
`required`, `provided`, and `value` only for plain vars), `resources` (the apply steps in order,
`{kind, name, action, detail}`; the first is the image push, `kind: image, action: push`, after
`ecr:Repository` on ECS), `preflight` (`{name, status: pass | warn | fail, detail, overridden}`;
`image` reads "Built and smoke-tested (sha256:...)"), `allow_missing_env`, `expected_url`,
`rendered` (manifests as YAML or task definition and service as JSON, secrets as `********`),
`rendered_format`, and `rollback_to_revision` when this is a rollback.

Nothing is pushed before approval, so there is no `push` stage. When the env changes at the
approval card (`POST /deploys/{id}/env`, M6), the pending approval is decided as `rejected`,
`approval` finishes `skipped`, `preflight` runs again, and a new `approval_requested` follows
with the new plan and the same `source_image`.

### `RunFinished`

`status`, `stop_reason` (including `no_progress`), `error`, `live_url`, `build_attempts`,
`automatic_fix`, `duration_ms`, `cost_usd`, `ultra_equiv_usd`.

## Example: a kind deploy with one automatic fix

```
event: stage_started     data.stage=scout
event: step              kind=llm  detect_app_spec on Nano
event: stage_finished    data.summary="Flask on port 8000, health /health"
event: stage_started     data.stage=build
event: step              kind=llm  write_dockerfile on Super
event: build_attempt     data.attempt=1 ok=false error_summary="pg_config executable not found"
event: step              kind=llm  fix_build on Super
event: build_attempt     data.attempt=2 ok=true dockerfile_source=fixed
event: stage_finished    data.stage=build
...                      smoke, plan, preflight (image built and smoke-tested, not pushed)
event: approval_requested data.type=deploy_plan
event: status            status=waiting_approval
event: approval_decided
event: stage_started     data.stage=apply   (push the image first, then the manifests)
event: stage_finished    data.stage=apply
event: verify_result     data.ok=true
event: live_url          data.url=http://legacy-flask-app.localhost
event: run_finished      data.automatic_fix=true
event: done
```

Older clients that only listen for `step`, `approval_*`, `status`, and `done` ignore the new event
names (EventSource only delivers named events to their listeners): the generic run page keeps
working for deploy runs, without the deploy events, until the Deploy page (Milestone 7) handles them.
