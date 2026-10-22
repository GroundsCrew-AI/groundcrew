# Groundcrew agent: progress

Brief: `docs/prompts/agent.md`. Plan: `docs/agent-plan.md`.

| Milestone | State | PR |
|---|---|---|
| A0 Plan | done | (on the `agent` branch) |
| A1 Enroll and report | live (release `45620dfed954`); real VM install still to try | #63 (draft) |
| A2 Signals and Medic | | |
| A3 Approved fixes | | |
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
