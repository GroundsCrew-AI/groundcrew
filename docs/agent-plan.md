# Groundcrew agent: plan

Brief: `docs/prompts/agent.md`. Progress: `docs/agent-progress.md` (from A1 on).

## Why an agent

Groundcrew reaches an environment today by calling its APIs: an AWS role, a cluster's
kubeconfig, a Vercel token. That leaves out plain VMs (Docker, Compose, systemd services on any
cloud or on-premises), private clusters with no public API, and hosts behind NAT. The agent
reverses the direction: it runs inside the environment and dials out to Groundcrew over HTTPS,
so nothing listens for Groundcrew and no cloud credential is handed over.

Medic, incidents, approvals, notifications, and builds stay as they are. The agent plugs in at
the four seams the cloud connections already use:

| Seam | Today | Agent |
|---|---|---|
| Connection (`app/connectors/`) | aws, kubernetes, github, vercel | new kind `agent`: enrollment, keys, last seen |
| Detection (`app/incidents/watcher.py`) | reads signals from cloud APIs on a timer | rules over the events agents sent |
| Evidence (`app/telemetry/collect.py`) | per-source readers over cloud APIs | `agent` reader over stored agent events and logs |
| Acting (`app/actions/`, `app/targets/`) | `EmergencyActions`, `Target` | `AgentActions`, `AgentTarget`: signed commands the agent runs |

## Shape

```
 VM                                                   groundkrew.com
┌──────────────────────────────────┐  HTTPS, out only ┌──────────────────────────────┐
│ groundcrew-agent (Go, systemd)   │ ───────────────▶ │ api: /agent/v1/*             │
│  config: /etc/groundcrew-agent/  │  heartbeat,      │  agents, agent_events tables │
│    agent.yaml (what to watch,    │  inventory,      │  → resources on the Map      │
│    what it may do)               │  events, logs    │  → watcher rules → incidents │
│  watches Docker / Compose /      │                  │  → Medic evidence            │
│    systemd: state, restarts,     │ ◀─────────────── │ commands (long poll), signed │
│    OOM, health URL, error logs   │  approved,       │  with Groundcrew's key after │
│  runs: restart, roll back image, │  signed commands │  a person approves           │
│    deploy image                  │                  │                              │
└──────────────────────────────────┘                  └──────────────────────────────┘
```

## Configuration file

`/etc/groundcrew-agent/agent.yaml`, owned by root, read by the agent:

```yaml
groundcrew:
  url: https://api.groundkrew.com
services:
  - name: legacy-flask-app
    runtime: docker            # docker | compose | systemd
    container: flask-app       # docker: container name; compose: project + service; systemd: unit
    health:
      url: http://127.0.0.1:8000/health
      interval: 30s
      timeout: 5s
    logs: container            # container | journald | a file path
    deploy:
      image: ghcr.io/acme/flask-app   # repository the agent may deploy from
    allow: [restart, rollback]        # the ceiling for this service; [] = watch only
```

- `allow` is enforced on the machine. Groundcrew cannot make an agent do anything its own
  configuration does not allow, even with a valid signed command. Default: watch only.
- Unknown keys are an error (no silent typos). `groundcrew-agent check` validates the file and
  tests each health URL, container, and unit without contacting Groundcrew.

## Protocol (`/agent/v1`, JSON over HTTPS)

**Enrollment.** An owner creates an agent connection on the Connections page and gets a
one-time join token (single use, expires in 1 hour, stored hashed). On the VM:
`groundcrew-agent enroll --token gcj_...`. The agent generates an Ed25519 key pair; the private
key never leaves the VM (`/var/lib/groundcrew-agent/key`, 0600). `POST /agent/v1/enroll` sends
the token, the public key, hostname, OS, architecture, and agent version, and returns the agent
id plus Groundcrew's command-signing public key, which the agent pins.

**Requests.** Every later request is signed by the agent's key: method, path, a timestamp, a
nonce, and the body's SHA-256. The server keeps only public keys, so a database leak does not let
anyone pose as an agent. Requests older than 5 minutes or with a seen nonce are refused.
Revoking the connection refuses the key at once.

**Reports.**
- `POST /heartbeat` every 30 s: version, uptime, config hash. Missed for 3 minutes: the agent
  shows as offline (a signal of its own; not an incident by default).
- `POST /inventory` on start and on change: each configured service with its runtime, state,
  image and digest, ports, and the health check. Becomes `agent:service` resources (plus one
  `agent:host`) on the Map.
- `POST /events` batched every 10 s: state changes, restarts, exit codes, OOM kills, health
  results (failures each time, successes summarized per minute), and error log lines. Logs are
  redacted on the VM before they leave it (same patterns as `app/security/redact.py`) and capped
  per minute. Stored 7 days.

**Commands.** `GET /commands?wait=25` is a long poll (nginx already serves long-lived API
requests). A command is `{id, action, service, params, plan_hash, expires_at}` signed with
Groundcrew's command key (Ed25519; the private key lives with the other server secrets in
`/etc/groundcrew/`). The agent checks the signature, the expiry, that it never ran this id,
and its own `allow` list, then runs it and posts the result to `POST /commands/{id}/result`.
The approval seal (`app/policy/actions.py`) stays as it is on the server: a command is issued
only from a spent `ApprovedAction`, and its plan hash is the one the person approved.

## Actions (VM, first runtime)

| Action | Docker | Compose | systemd |
|---|---|---|---|
| `restart` | `docker restart` | `docker compose restart <svc>` | `systemctl restart <unit>` |
| `rollback` | recreate the container from the previous image digest the agent recorded | same, with an override file pinning the previous digest | not offered |
| `deploy` | pull the new image (registry credentials passed with the command, never stored), recreate, wait for health, go back to the previous digest if it fails | same | not offered |

The agent records the last few image digests per service so a rollback has a target that
exists on the VM. Builds stay in Groundcrew's isolated build service; the agent only pulls.

## Detection rules (server side)

Rules run over stored events, like the cloud rules: health check failing N times in a row, a
restart loop, an OOM kill, an exit with a non-zero code, an error log spike (the existing log
rule's thresholds). Rules live on the server so they can change without upgrading agents.

## Security

- Out only: no port opens on the VM; Groundcrew never connects in.
- The agent runs as its own user `groundcrew-agent`. Docker access means membership of the
  `docker` group, which is root-equivalent on that VM; the install guide says so plainly, and
  systemd-only setups get a polkit rule limited to the listed units instead.
- Watch only by default; every action needs the service's `allow` list, a person's approval in
  Groundcrew, and a valid, unexpired, unused, signed command.
- No secret values leave the VM: environment values are never read for reports; logs are
  redacted on the VM.
- Workspace isolation as for every connection: agent rows, events, and commands are scoped by
  the connection's workspace; the cross-workspace route tests cover the new routes.
- Limits: agents per workspace, events and log lines per minute per agent, request size, and
  nginx rate limits on `/agent/`.

## Code layout

- `apps/agent/` — Go module `github.com/GroundsCrew-AI/groundcrew/apps/agent`: `cmd/groundcrew-agent`,
  `internal/config`, `internal/client` (signing, retries), `internal/runtime/{docker,compose,systemd}`,
  `internal/watch`, `internal/commands`. Standard library plus the Docker Engine API over its
  socket (plain HTTP, no Docker SDK), and `gopkg.in/yaml.v3`. Static binary for linux/amd64 and
  linux/arm64.
- Built and tested in the `golang` Docker image (no local Go needed): `make agent`,
  `make agent-test`.
- API: `app/agents_remote/` (routes, signatures, storage), migration for `agents`,
  `agent_events`, `agent_commands`; reader `app/telemetry/agent.py`; rules
  `app/incidents/agent_rules.py`; `app/actions/agent.py`; `app/targets/agent.py`.
- Web: Connections → "Install an agent" (token, install commands, status), agent hosts and
  services on the Map, the incidents and deploy pages unchanged.
- Install: `install.sh` served from groundkrew.com downloads the binary and its SHA-256 sum,
  installs the systemd unit, and runs `enroll`. Releases are built by `make agent-release` and
  published to GitHub Releases (no CI for now, as in Phase 6).

## Milestones (draft PR and stop after each)

- **A1 Enroll and report.** Go agent: config, `check`, `enroll`, heartbeat, inventory for Docker
  and systemd. API: join tokens, enrollment, signed requests, heartbeat and inventory, agent
  resources. Web: install dialog, online/offline, agent services on the Map. Tests: Go unit
  tests; API tests for signatures, replay, revocation, isolation; an end-to-end test with the
  agent in a container against the local stack.
- **A2 Signals and Medic.** Events and logs (redacted on the VM), server rules, incidents for
  agent services, the evidence reader so Medic can diagnose them.
- **A3 Approved fixes.** Command signing, the long poll, `restart` and `rollback` on the VM,
  verify after the fix. Medic proposes them like the cloud fixes.
- **A4 Deploys.** `AgentTarget`: deploy a built image from the Deploy page through the agent,
  with health check and automatic return to the previous digest.
- **A5 Compose and packaging.** Compose runtime, `install.sh`, release builds, docs pages
  (connect an agent, permissions, troubleshooting). Later: a Kubernetes and an ECS packaging of
  the same agent.

## What you do by hand

- A1 end to end on a real VM: any small Linux VM with Docker (an EC2 t3.micro in the
  Groundcrew account, or elsewhere). I can propose the Terraform, but creating it needs your OK.
- Production rollout of each milestone: your OK, as for every deploy.

## Open questions

1. Join token lifetime (1 hour) and agents per workspace (proposed 10): fine?
2. Event retention 7 days: fine?
