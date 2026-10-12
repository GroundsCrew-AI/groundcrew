# Phase 5 progress (Medic)

Owner: Ahmed. Plan: `docs/prompts/phase5-ahmed.md`. Contract: `docs/phase5-interfaces.md`.

## Status

| Milestone | Branch | PR | State |
|---|---|---|---|
| M0 Interfaces | `phase5-m0-interfaces` | #22, #23 | Merged 2026-10-12 |
| M1 Telemetry readers | `phase5-m1-telemetry` | #25 | Merged 2026-10-12 |
| M2 Detection | `phase5-m2-detection` | #26 (draft) | Done, in review |
| M3 Medic agent | | | |
| M4 Incidents API + wiring | | | |
| M5 Live verification | | | Needs Rizwan: checkout-demo rebuild, Operate role |

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
- Resource group = affected resources plus one-hop graph neighbors in either direction. The api
  and the worker share the database, so an RDS spike and a worker crash become one incident.
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

## How to resume

```bash
cd ~/Personal/groundcrew && git fetch && git checkout <branch>
make db
cd apps/api && UV_PROJECT_ENVIRONMENT=~/Personal/envs/groundcrew uv run pytest -q -m "not docker"
```
