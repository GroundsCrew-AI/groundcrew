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
- API: `app/remote_agents/` (routes, signatures, inventory), migrations for `agents` and
  `agent_nonces` (A1), `agent_events` and `agent_commands` later; reader `app/telemetry/agent.py`; rules
  `app/incidents/agent_rules.py`; `app/actions/agent.py`; `app/targets/agent.py`.
- Web: Connections → "Install an agent" (token, install commands, status), agent hosts and
  services on the Map, the incidents and deploy pages unchanged.
- Install: `install.sh` served from groundkrew.com downloads the binary and its SHA-256 sum,
  installs the systemd unit, and runs `enroll`. The web image builds the binaries at each
  release (`apps/web/Dockerfile`, build context `agent-src`), so no separate hosting is needed.

## Milestones (draft PR and stop after each)

- **A1 Enroll and report.** Go agent: config, `check`, `enroll`, heartbeat, inventory for Docker,
  Compose, and systemd. API: join tokens, enrollment, signed requests, heartbeat and inventory,
  agent resources. Web: install dialog, online/offline, agent services on the Map, docs page.
  Downloads: the web image builds the agent and serves it with `SHA256SUMS`, `install.sh`, the
  systemd unit, and an example configuration at groundkrew.com/agent/ (moved here from A5 so
  A1 can be installed on a real server). Tests: Go unit tests; API tests for signatures, replay,
  revocation, isolation; a shared signing vector; an end-to-end run of the binary against a
  live API; `install.sh` in a clean Debian container.
- **A2 Signals and Medic.** Events and logs (redacted on the VM), server rules, incidents for
  agent services, the evidence reader so Medic can diagnose them.
- **A3 Approved fixes.** Command signing, the long poll, `restart` and `rollback` on the VM,
  verify after the fix. Medic proposes them like the cloud fixes.
- **A4 Deploys.** `AgentTarget`: deploy a built image from the Deploy page through the agent,
  with health check and automatic return to the previous digest.
- **A5 Packaging.** Signed release builds, a Kubernetes and an ECS packaging of the same agent
  (one milestone, decided 2026-10-23; plan below in "A5 plan").

## What you do by hand

- A1 end to end on a real VM: any small Linux VM with Docker (an EC2 t3.micro in the
  Groundcrew account, or elsewhere). I can propose the Terraform, but creating it needs your OK.
- Production rollout of each milestone: your OK, as for every deploy.

## Decisions

- Join tokens work once, for 1 hour (AGENT_JOIN_TOKEN_MINUTES). Agents count as connections, so
  the workspace's connection quota caps them. Event retention: 7 days (A2).

## A5 plan (2026-10-23)

Decisions (2026-10-23): releases are signed with an Ed25519 key the owner holds; Kubernetes and
ECS come in one milestone; Groundcrew as an MCP server is planned separately (`docs/mcp-plan.md`).

### 1. Signed releases

Today the web image builds the agent on the server and `install.sh` checks only SHA-256 sums
served next to the binaries: anyone who can change what groundkrew.com serves can change both.

- **Key.** One Ed25519 key pair, made with `openssl genpkey -algorithm ed25519`. The private key
  stays in `ops/agent-release.key` (git-ignored, on the owner's machine only). The public key is
  committed (`apps/agent/packaging/release.pub`), embedded in `install.sh`, and compiled into
  the agent.
- **Reproducible builds.** The binaries are built in a golang image pinned by digest, with
  `-trimpath -buildvcs=false -ldflags "-s -w -buildid= -X main.version=..."`, so the same source
  gives the same bytes on any machine.
- **Signing, on the owner's machine:** `make agent-release` builds both binaries, writes
  `apps/agent/release/SHA256SUMS`, and signs it to `SHA256SUMS.sig` with the private key. Both
  files are committed with the version bump. Nothing secret is committed.
- **Serving.** The web image builds the binaries again and **fails the build** if its sums
  differ from the committed `SHA256SUMS`. So the server can only serve exactly what was signed.
  It serves the committed `SHA256SUMS` and `SHA256SUMS.sig` next to the binaries and a container
  image (below).
- **Checking.** `install.sh` verifies `SHA256SUMS.sig` with the embedded public key
  (`openssl pkeyutl -verify -rawin`, OpenSSL 3, on every current distribution) before it trusts
  any sum, and refuses on any mismatch. An installed agent checks a new release with its compiled-in
  key (`groundcrew-agent update`), so an older agent never runs an unsigned newer one.
- **Key loss or leak:** a new key needs a new agent release signed with the old key that carries
  both public keys, then a release with the new key only. Written down in the runbook.

### 2. The agent's container image

Kubernetes and ECS run the agent as a container: `groundcrew-agent:<version>` (distroless static
base, non-root, the same signed binary). It is served from the Groundcrew registry under a
read-only public path, `api.groundkrew.com/public/groundcrew-agent`: anonymous `GET`/`HEAD`
for that repository only (nginx), pushes only from the release step on the server. Every other
repository keeps its token checks. The release step pushes it and records its digest, and the
manifests and template pin the digest.

### 3. Kubernetes

The agent runs inside the cluster and dials out, so private clusters with no public API work.

- **Runtime `kubernetes`**, a service is `namespace` + `deployment` (StatefulSets later):

  ```yaml
  services:
    - name: checkout-api
      runtime: kubernetes
      kubernetes: {namespace: shop, deployment: api, container: api}
      health: {url: http://api.shop.svc:8000/health}
      allow: [restart, rollback, deploy]
  ```

- **Reads:** the Deployment, its ReplicaSets and Pods: ready and available replicas, restarts,
  OOM kills, last exit code, the image and digest, the ReplicaSet before it (the rollback
  target), pod logs (redacted on the way). Plain REST to the API server with the
  ServiceAccount token: no client-go, so the binary stays small.
- **Actions,** each only if `allow` lists it:
  - `restart`: the `kubectl rollout restart` annotation, then waits for the rollout.
  - `rollback`: sets the container image back to the previous ReplicaSet's, waits for the rollout.
  - `deploy`: sets the image (with an image pull secret made from the one-time registry login,
    in that namespace only), waits for rollout and health, and goes back to the previous image if
    it fails. A missing Deployment is created from a minimal template (the app's port, one
    replica, a ClusterIP Service), like the agent does for a missing container.
- **Packaging:** `kubernetes/agent.yaml` (served at groundkrew.com/agent/): a Namespace
  `groundcrew-agent`, a ServiceAccount, a Deployment with one replica, a 1 GiB PVC for the
  agent's key and state, a ConfigMap holding `agent.yaml`, and per watched namespace a Role
  plus RoleBinding. Read verbs are always granted. `patch` on deployments, and `create` on
  deployments, services and secrets, are granted only for namespaces whose services allow
  `deploy` or `rollback`. The install dialog writes the file with the join token as a one-time
  Secret.
- **Tests:** a fake API server for unit tests; a live run on the kind cluster (`GC_KIND_TEST=1`)
  that enrolls, reports, restarts, deploys, and rolls back a Deployment.

### 4. ECS

- **Runtime `ecs`**, a service is `cluster` + `service`. Reads: the service, its deployments,
  running and pending counts, task stops and exit codes, the task definition's image, the
  revision before it, and CloudWatch log lines through the task definition's log
  configuration. Actions: `restart` (force a new deployment), `rollback` (the service back to the
  previous task definition revision), `deploy` (a new revision with the new image, then
  `UpdateService`, wait for steady state and health, back to the previous revision on failure).
- **Calls:** AWS APIs with SigV4 signed in the agent (standard library, about 150 lines).
  Credentials come from the task role through the ECS credentials endpoint; the agent never
  holds keys.
- **Packaging:** `ecs/agent.yaml`, a CloudFormation template (Quick-create link like the AWS
  connection) that makes a Fargate service in the chosen cluster, the task role, the log group,
  and the join token in Secrets Manager (read once). The task role can only describe, read logs,
  and `UpdateService` / `RegisterTaskDefinition` / `iam:PassRole` for services tagged
  `groundcrew:managed=true`, so it can't touch anything else in the account.
- **Tests:** SigV4 against AWS's published test vectors; a fake ECS endpoint for the actions;
  a live run in account 881050425883 only with the owner's OK (AWS writes).

### 5. Server side

The API changes little: services report the same states and take the same commands. New parts
are the runtime and kind columns on the Map and the connection, `AgentTarget` plans that name
the Deployment or ECS service instead of a container, and the install dialog's Kubernetes and
ECS tabs. Medic's agent fixes already go through the runtime-neutral commands.

### What the owner does

- Make the release key once: `make agent-release-key` (writes `ops/agent-release.key` and
  prints the public key to commit), and keep a copy somewhere safe offline.
- Approve the first production release that serves the image, and the nginx change for the
  public image path.
- OK the live ECS test in 881050425883 (a Fargate service for about 10 minutes).

