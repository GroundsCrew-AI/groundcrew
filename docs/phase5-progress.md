# Phase 5 progress (Medic)

Owner: Ahmed. Plan: `docs/prompts/phase5-ahmed.md`. Contract: `docs/phase5-interfaces.md`.

## Status

| Milestone | Branch | PR | State |
|---|---|---|---|
| M0 Interfaces | `phase5-m0-interfaces` | #22 (draft) | Agreed with Rizwan; waiting for merge |
| M1 Telemetry readers | | | Not started (after M0 merges) |
| M2 Detection | | | |
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

## How to resume

```bash
cd ~/Personal/groundcrew && git fetch && git checkout <branch>
make db
cd apps/api && UV_PROJECT_ENVIRONMENT=~/Personal/envs/groundcrew uv run pytest -q -m "not docker"
```
