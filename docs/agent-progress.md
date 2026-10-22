# Groundcrew agent: progress

Brief: `docs/prompts/agent.md`. Plan: `docs/agent-plan.md`.

| Milestone | State | PR |
|---|---|---|
| A0 Plan | done | (on the `agent` branch) |
| A1 Enroll and report | live (release `45620dfed954`); real VM install still to try | #63 (draft) |
| A2 Signals and Medic | built with A3, tested locally, not deployed | #63 |
| A3 Approved fixes (restart) | built with A2, tested locally, not deployed | #63 |
| A4 Deploys | | |
| A5 Packaging | | |

## A1 Enroll and report (2026-10-22)

Done:
- Go agent `apps/agent` (Go 1.23, one dependency: yaml.v3; built and tested in the golang image
  with `apps/agent/go.sh`, `make agent`, `make agent-test`). Commands `check`, `enroll`, `run`,
  `version`. Strict configuration (unknown keys and every problem reported at once). Inventory
  of Docker containers and Compose services through the Engine API socket, and systemd units
  through `systemctl show`. Ed25519 key in `/var/lib/groundcrew-agent` (0700/0600), written only
  after Groundcrew accepted the enrollment. Heartbeat every 30 s; inventory when it changes and
  every 15 minutes; 5-minute backoff after a refusal.
- API `app/remote_agents`: join tokens (hash only, single use, 1 hour), `/agent/v1/enroll`,
  signed `/agent/v1/heartbeat` and `/agent/v1/inventory` (signature over method, path,
  timestamp, nonce, body hash; 5-minute skew; nonce replay table; per-agent rate limit after
  the signature). Inventory is stored on the connection and starts a discovery of it, so
  `agent:host` and `agent:service` resources reach the Map through the normal discovery (stale
  handling included). Connection kind `agent` (counts against the connection quota), `AgentOut`
  (enrolled, online, host, version, services), owner-only `POST /connections/agent` and
  `POST /connections/{id}/agent/token`. Migration `7a1c4e2b9d30` (agents, agent_nonces).
  Production refuses to start with AGENT_API_URL on localhost.
- Web: Agent card on Connections (replaces "VM: not available yet"), install dialog with the
  one-time command and live "Connected" status, agent rows (waiting, online, offline, New join
  token), map icons and labels, docs page `/docs/connect/agent`.
- Downloads: the web image builds the agent for linux/amd64 and arm64 and serves it with
  `SHA256SUMS`, `install.sh`, the systemd unit (hardened), and an example configuration at
  `/agent/` (public paths). `gc-release` passes `--build-context agent-src=apps/agent`.
- nginx: `location /agent/v1/` on the API domain with its own per-address limit (gc_agent).

Checked:
- API tests (tests/test_remote_agents.py, tenant isolation, platform host, production
  lockout), Go tests (config, signing, inventory with a fake Docker socket, state, run loop), a
  signing vector shared by the Go and Python tests, web tests and typecheck.
- End to end on this machine: the compiled agent against a live API on a fresh database
  migrated with Alembic: `check` found a real container, a Compose service, and a systemd unit
  (and named the missing one); enroll, token reuse refused (the working key kept), run, then
  the discovery put the host and four services on the Map; the connection showed online.
- `install.sh` in a clean Debian container against the built downloads: checksum, user, files
  and permissions, enroll, unit enabled, agent reporting; a second run upgrades and keeps the
  configuration. shellcheck clean.

Rollout (2026-10-22, with your OK): `push-host 40-nginx` (agent location), `push-host 50-app`
(gc-release with the agent build context), then release `45620dfed954` from the `agent` branch
(`516fa029aa8e` kept for rollback); migration `7a1c4e2b9d30` ran; AGENT_API_URL set.
Live checks: groundkrew.com/agent/ serves install.sh, SHA256SUMS (same sums as the local build),
the unit, the example configuration, and both binaries; /agent/v1/enroll refuses a bad token and
/agent/v1/heartbeat an unsigned request (401); other API routes still 404 from outside;
/docs/connect/agent is up.

Open issues:
- Not yet tried on a real VM against groundkrew.com.
- Production now runs the `agent` branch (it includes everything on `phase6-deploy`): merge
  #62 and then #63, or deploy from `phase6-deploy` only after #63 is merged into it.

## A2 + A3 Signals, Medic, and restarts (2026-10-22)

Built together toward one test, at your request: stop a container, Medic proposes a restart,
you approve, the agent restarts it, Medic resolves.

Done:
- Agent (Go): `internal/runtime` (Docker / Compose through the Engine API, systemd through
  systemctl and journalctl: state, logs, restart), `internal/watch` (every 10 s: each service's
  state with consecutive failed health checks, and events: state changes, failed and recovered
  health checks, restarts, image changes, error log lines redacted on the host by
  `internal/redact`; events kept while Groundcrew is unreachable), `internal/commands` (long
  poll; runs a command only if signed by the pinned Groundcrew key, for this agent, not
  expired, not seen, and allowed for the service; reports the result; the watcher reports at
  once afterwards). The command key is pinned on first sight from the heartbeat and cleared on
  a new enrollment.
- API: migration `c3e8f1a2b4d6` (agent_events, agent_commands, agents.states/firing).
  `/agent/v1/events` stores states and events (7-day retention, redacted again) and opens an
  incident when a rule starts firing for a service: `agent_service_down` (new detector rule,
  critical), `health_check` (3 failed in a row), `log_error_spike` (10 error lines in 5 min);
  only with INCIDENT_WATCH_ENABLED. `/agent/v1/commands/poll` (25 s long poll, handed out once,
  expired ones marked) and `/agent/v1/commands/{id}/result`. Commands are signed with an Ed25519
  key derived from APPROVAL_TOKEN_KEY (no new secret).
- Medic: emergency action `agent_restart_service` (`app/actions/agent.py`, behind the router;
  needs an online agent and `allow: [restart]`; refuses if the state moved since the plan; waits
  up to 2 minutes for the agent's result). Medic proposes it for agent services that are down or
  fail health checks; evidence reader `app/telemetry/agent.py` (events, state now, image changes
  as deploy markers); verify reads the agent's reports after the fix.
- Web: rule and fix labels; docs page section "Incidents and fixes". Installer adds the agent's
  user to systemd-journal.

Checked:
- API: tests/test_agent_signals.py (rules and transitions, redaction on arrival, watch switch
  off, commands handed out once, expiry, result for another agent's command 404, plans refused
  without allow / offline / unknown service, execute through a signed command, no run after the
  state moved, Medic end to end to "resolved"); a command-signing vector shared with the Go tests.
- Go: runtime against a fake Docker socket (state, demultiplexed logs, restart), systemd with a
  fake runner, watcher (events, redaction, health recovery, image change, kept while offline),
  commands (vector from the API, every refusal reason, no replay).
- Live on this machine: the built agent against a local API (fresh migrated database) watching
  a throwaway nginx container. `docker stop` opened a critical incident in about 4 s with the
  failed health checks and the exit as evidence; the approved restart reached the agent by long
  poll and the container ran again 4 s later; then "health recovered after 7 failed checks" and
  the rule stopped firing. Found and fixed on the way: an empty event list was sent as `null`.

Not done yet: rollback and deploy through the agent (A4), restarting systemd units needs a
polkit or sudo rule for the agent's user.
