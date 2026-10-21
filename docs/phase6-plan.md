# Phase 6 plan: groundkrew.com on one EC2 instance

The P6.1 plan. Nothing in AWS, DNS, GitHub settings or the server changes in this milestone.
Brief: `docs/prompts/phase6.md`. Progress: `docs/phase6-progress.md`.

## 1. Summary

- **Instance:** `t3.large` (2 vCPU, 8 GiB) in unlimited credit mode, 4 GiB swap, 80 GiB encrypted gp3.
  About **$71 a month**, up to about $81 with many builds (section 3). `t3.xlarge` (about $134) only if builds turn out to be the bottleneck.
- **Layout:** nginx and certbot on the host are the only public listeners. web, API, worker and Postgres run
  under the main Docker daemon (Compose) and publish only to `127.0.0.1`. Builds run in a second, rootless
  Docker daemon owned by the system user `gcbuild`, with gVisor for every container it runs.
- **Biggest risk:** rootless Docker plus gVisor is not a supported combination upstream, and `docker build` runs
  `RUN` steps with BuildKit's own runc, not the daemon's runtime. The design below works around both
  (BuildKit runs *inside* a gVisor container, one per build), but it needs a spike on Ubuntu 24.04 before
  P6.5 is written. A fallback for you to choose now is in section 6.6.
- **Already in the code** (reused, not rebuilt): accounts with GitHub, Google and email; invites by email or
  GitHub login; per-workspace and global LLM caps (`llm_usage`, `llm_workspace_usage`); an in-process rate
  limiter on the sign-in routes; the `APP_ENV=production` start-up check (`Settings.production_problems`);
  `/ready`; SMTP mail; connection delete; CORS for `WEB_ORIGIN`; dev-only gates on most dev paths.
- **Missing** (built in P6.2 to P6.6): roles `approver` / `viewer`, magic links, a GitHub webhook receiver,
  most quotas, the signup switch, security headers and CSRF checks, workspace and account deletion,
  public pages, a production web image, the build daemon, Terraform, host scripts, release pipeline, updater.

## 2. What the code has today and what each milestone changes

| Area | Today | Phase 6 |
|---|---|---|
| `APP_ENV` | `production` by default; `staging`/`production` refuse to start without the required secrets | P6.2: also refuse dev-only settings (`LIVE_SCRIPT_SELF_APPROVE`, `PIPELINE_FAKE`, `E2E_TEST_AUTH`, `DEPLOY_FIXTURES_DIR`, `K8S_ALLOW_PRIVATE_SERVERS`) |
| Dev-only paths | `require_dev` (scripts), `/workspaces/{id}/evidence` 404, self-approve, fixtures, pipeline fake, test auth (web), private kind servers | P6.2: dev routers not mounted at all in production, plus one test per path proving it refuses (list in section 9) |
| Roles | `owner`, `member` (any member approves) | P6.2: `owner`, `approver`, `viewer`; migration maps `member` to `approver` (today's behaviour). Viewers get 403 on writes; non-members keep 404 |
| Invites | email or GitHub login, role owner/member | P6.2: the three roles; pending invites listed with who sent them |
| Approvals audit | `approvals.decided_by`, `decided_at` | P6.2: an `audit_events` table (who, what, when, workspace, target, IP) for approvals, role changes, invites, deletions, connection changes, signup switch; shown in Settings |
| Sign-in | GitHub, Google, email and password | P6.3: email magic links (single use, 15 min, hash stored, like `auth_tokens`). Decision 6.1 on passwords and Google |
| GitHub App | one dev app; no webhook receiver | P6.3: production app settings; `POST /github/webhook` with `X-Hub-Signature-256` check (installation added / removed / suspended) |
| AWS connect | Quick-create link from S3 | P6.3: download `https://groundkrew.com/aws-connect.yaml`, "Upload a template file", parameters with Copy buttons, and the `aws cloudformation create-stack` command; Verify unchanged |
| Spend | per-run, per-workspace daily, global daily and total USD caps | P6.2: the "limit reached" message names the cap and when it resets; Settings shows usage |
| Quotas | Tavily per incident | P6.2: Tavily per workspace per day, concurrent runs, builds per day, max build time, max connections; all `QUOTA_*` settings with per-workspace overrides in `workspaces.settings` |
| Rate limits | in-process limiter on sign-in routes | P6.2: also per user on the API and on stream tokens; P6.5: nginx `limit_req` / `limit_conn` per IP |
| Headers | `poweredByHeader: false` | P6.2: CSP with a nonce (Next middleware), HSTS, `frame-ancestors 'none'`, Referrer-Policy, Permissions-Policy; same on the API. Cookies `__Secure-`/`__Host-` prefixed, HttpOnly, SameSite=Lax |
| CSRF | Auth.js protects its own routes; server actions check Origin | P6.2: the `/api/gc` proxy refuses non-GET requests whose `Origin` is not `WEB_ORIGIN` or whose `Sec-Fetch-Site` is not `same-origin` |
| Deletion | connection delete | P6.2: connection delete also removes its resources, edges, events and stored secrets; workspace delete (owner, typed name); account delete (blocked while you are the only owner of a shared workspace) |
| Public pages | none (all behind sign-in) | P6.2: `/` landing, `/terms`, `/privacy`, `/security`, `/docs/connect/{aws,github,vercel,kubernetes}`; the app moves under its existing routes, signed-in users land on the overview |
| Web image | `Dockerfile.dev` only | P6.6: `Dockerfile` with `output: "standalone"`, non-root, read-only root |
| AWS identity | IAM user `groundcrew-platform` with keys | P6.4: instance role `groundcrew-platform`; keys removed |

Teammate areas touched (listed in each PR, per the ownership notes): `app/tools/tavily.py` (daily Tavily quota),
`app/sandbox/docker.py` (build and smoke through the isolated daemon), `app/agents/medic/` only if the quota
message needs a hook there.

## 3. Instance size and monthly cost

### Memory budget (limits in Compose; the build daemon gets its own systemd slice)

| Process | Limit | Expected use |
|---|---|---|
| Ubuntu, sshd, journald, fail2ban, unattended-upgrades | | 400 MB |
| nginx | 128 MB | 20 MB |
| Postgres 16 + pgvector (`shared_buffers` 512 MB, `max_connections` 60) | 1.5 GB | 0.7 to 1.2 GB |
| API (uvicorn, 2 workers) | 768 MB | 400 MB |
| worker (one process; git clones; Docker CLI) | 1 GB | 500 MB |
| web (Next standalone) | 512 MB | 250 MB |
| Build slice (rootless dockerd + one build + one smoke container) | 3.5 GB | 0 when idle, up to 3.5 GB |
| **Total** | **7.4 GB of 8 GiB** | **2.3 GB idle, about 6 GB during a build** |

Swap 4 GiB (a swap file, `vm.swappiness=10`) covers a heavy JVM or .NET build. One build at a time on the host
(`SANDBOX_MAX_CONCURRENT_BUILDS=1`, queued in order); per-workspace limits sit on top of that.

### CPU

`t3.large` earns 0.6 vCPU of sustained baseline (30% of 2). Idle load (API, worker polling, Postgres, watcher
jobs) is around 0.1 to 0.2 vCPU, so the instance banks credits between builds and spends them during builds.
Unlimited mode means it never throttles; extra use above baseline costs $0.05 per vCPU-hour. Twenty
10-minute builds a day at 2 vCPU above what credits cover would be at most about $10 a month; a normal day
costs nothing extra.

### Disk: 80 GiB gp3 (3,000 IOPS, 125 MB/s, the free baseline)

| Use | Size |
|---|---|
| Ubuntu, packages | 6 GB |
| Main Docker: two releases of the app images, base images | 6 GB |
| Postgres (named volume) | 10 GB of room (embeddings 4 KB each; run steps grow fastest) |
| Build daemon data, on its own 30 GB filesystem (section 6.4) | 30 GB |
| Logs (journald capped at 2 GB, nginx logs 30 days) | 3 GB |
| Free headroom | about 25 GB |

### Monthly cost (us-east-2, on-demand list prices, 730 hours)

| Item | Price | Per month |
|---|---|---|
| `t3.large` | $0.0832 per hour | $60.74 |
| gp3 80 GiB | $0.08 per GB-month | $6.40 |
| Elastic IP (public IPv4, charged while attached) | $0.005 per hour | $3.65 |
| Data out | first 100 GB free each month, then $0.09 per GB | $0 expected |
| t3 unlimited surplus credits | $0.05 per vCPU-hour | $0 to $10 |
| IAM role, security group | free | $0 |
| **Total** | | **about $71 to $81** |

Outside AWS: domain (already yours), SMTP (Resend's free tier is 3,000 emails a month; Gmail SMTP is free with
an app password but limited to about 500 a day and needs your own Google account), Token Factory and Tavily
usage (bounded by the caps). Prices: the `t3.large` rate is confirmed against a third-party listing; gp3, IPv4
and data out are AWS list prices as known; P6.4 re-reads all of them from the AWS Price List before apply.

A 1-year Compute Savings Plan would bring the instance to about $38 a month; not proposed now.

## 4. Terraform (`infra/platform/`)

```
infra/platform/
  versions.tf        terraform >= 1.9, aws provider ~> 6.x, pinned in .terraform.lock.hcl
  backend.tf         S3 backend, key "groundcrew/platform.tfstate" (decision 6.4)
  providers.tf       region us-east-2, allowed_account_ids = ["881050425883"], default_tags
  variables.tf       admin_cidr, ssh_public_key, instance_type, volume_gb, ubuntu_ami_owner
  main.tf            the five resources below
  iam.tf             instance role and profile
  cloud-init.yaml.tftpl
  outputs.tf         elastic_ip, instance_id, role_arn, dns_records (text to paste at the registrar)
  README.md
```

| Resource | Settings |
|---|---|
| `aws_instance.platform` | Ubuntu 24.04 LTS amd64 (latest Canonical AMI via `aws_ami` data source, owner 099720109477, pinned after first apply with `lifecycle.ignore_changes = [ami]`), `t3.large`, `credit_specification { cpu_credits = "unlimited" }`, `metadata_options { http_tokens = "required", http_put_response_hop_limit = 2, instance_metadata_tags = "disabled" }`, root `gp3` 80 GiB `encrypted = true` (AWS-managed key), `delete_on_termination = false`, `disable_api_termination = true`, `user_data` = cloud-init, default VPC subnet in one AZ |
| `aws_eip.platform` | `domain = "vpc"`, associated with the instance |
| `aws_security_group.platform` | in: 80 and 443 from `0.0.0.0/0` (and `::/0` only if we add AAAA records later), 22 from `var.admin_cidr`; out: all |
| `aws_iam_role.platform` + `aws_iam_instance_profile` | name `groundcrew-platform`; trust `ec2.amazonaws.com` only; one inline policy: `sts:AssumeRole` on `arn:aws:iam::*:role/groundcrew-*`. Nothing else |
| tags | `project=groundcrew`, `groundcrew:role=platform` on everything (provider `default_tags` plus volume tags) |

Notes:
- **No key pair resource:** the SSH public key goes into cloud-init (`ssh_authorized_keys`), so no extra AWS
  object is needed.
- **Hop limit 2** is needed so the API and worker containers can reach IMDS for the instance role. Which
  containers may reach it is enforced by the host firewall (section 5.4), not by the hop limit.
- **No secrets in state:** Terraform only sees the public SSH key and the admin CIDR. Secrets are generated on
  the server (section 5.5).
- `user_data` changes do not replace the instance (`user_data_replace_on_change = false`); a rebuild is an
  explicit `terraform apply -replace`.
- `terraform plan` runs read-only first and is shown to you; apply only after your OK (P6.4).

## 5. Host layout

```
                     Internet
                        │ 80, 443 (22 from admin IP only)
                 ┌──────┴───────────────────────────────────────────────────────────────┐
                 │ EC2 t3.large  Ubuntu 24.04  (SG + ufw)                                │
                 │                                                                      │
                 │  nginx (host)  ── groundkrew.com, www ──▶ 127.0.0.1:3000  web          │
                 │   certbot.timer └─ api.groundkrew.com ──▶ 127.0.0.1:8000  api (SSE)    │
                 │                                                                      │
                 │  main dockerd (root) ─ Compose project "groundcrew"                   │
                 │    network gc-backend 172.30.0.0/24 (internal: web, api, worker, pg)   │
                 │    network gc-egress  172.30.1.0/24 (api, worker: AWS, GitHub, LLMs)   │
                 │    postgres  (gc-backend only, no ports)                              │
                 │    api       127.0.0.1:8000   static IP, may reach IMDS               │
                 │    worker    no ports         static IP, may reach IMDS               │
                 │    web       127.0.0.1:3000   no IMDS                                 │
                 │                                                                      │
                 │  rootless dockerd (user gcbuild, uid 1500) ─ builds only              │
                 │    socket /run/gcbuild/docker.sock (ACL: worker's uid only)            │
                 │    data /var/lib/gcbuild (own 30 GB filesystem)                       │
                 │    runtime runsc (gVisor) for every container                         │
                 │    egress via the gcbuild uid: internet yes; IMDS, private ranges,    │
                 │    host addresses no (iptables owner match)                           │
                 └──────────────────────────────────────────────────────────────────────┘
```

### 5.1 Users and directories

| Path | Owner, mode | What |
|---|---|---|
| `/etc/groundcrew/` | root, 0700 | secrets and config |
| `/etc/groundcrew/app.env` | root, 0600 | API and worker secrets (Compose `env_file`, read by root at `up`) |
| `/etc/groundcrew/web.env` | root, 0600 | web secrets |
| `/etc/groundcrew/postgres.env` | root, 0600 | `POSTGRES_PASSWORD` |
| `/etc/groundcrew/github-app.pem` | uid 10001 (app user in the containers), 0400 | the one file a container must read directly; bind-mounted read-only into api and worker. The only exception to root-owned |
| `/etc/groundcrew/quotas.env` | root, 0600 | `QUOTA_*`, caps (not secret, but kept beside the rest) |
| `/opt/groundcrew/` | root, 0755 | `compose.yml`, scripts (`bin/gc-*`), the current release file; from `infra/platform/host/` at a release tag |
| `/var/lib/groundcrew/releases/` | root, 0700 | `current.json`, `previous.json` (image digests), updater state |
| `/var/lib/gcbuild/` | gcbuild, 0700 | rootless daemon data, on a loop-mounted XFS image (section 6.4) |
| `/var/log/groundcrew/` | root | updater and host-script logs (logrotate, 30 days) |

Containers run as uid 10001 (api, worker, web) and the image default for Postgres (uid 999). The worker gets
supplementary group `gcbuild-sock` so it can use the build socket and nothing else of `gcbuild`.

### 5.2 Compose (`infra/platform/host/compose.yml`)

Every service: `restart: unless-stopped`, a health check, `mem_limit` / `cpus` / `pids_limit`,
`security_opt: [no-new-privileges:true]`, `cap_drop: [ALL]`, logging driver `journald` with a `tag`, images
pinned by digest from the release file.

| Service | read_only | tmpfs | Health check | Notes |
|---|---|---|---|---|
| postgres (`pgvector/pgvector:pg16`, pinned digest) | no (data dir) | `/var/run/postgresql` | `pg_isready` | `scram-sha-256`, `listen_addresses='*'` on `gc-backend` only; volume `pgdata` |
| migrate (api image, one-shot) | yes | `/tmp` | | `alembic upgrade head`; api and worker depend on it completing |
| api | yes | `/tmp` | `GET /ready` | `uvicorn --workers 2 --proxy-headers`, forwarded headers trusted only from the Docker gateway (nginx on the host) |
| worker | yes | `/tmp`; clones in a named volume `worker-scratch`, wiped on start | heartbeat file age | `DOCKER_HOST=unix:///run/gcbuild/docker.sock` mounted read-only |
| web | yes | `/tmp`, `.next/cache` | `GET /api/health` | `AUTH_URL=https://groundkrew.com`, `AUTH_TRUST_HOST=true` |

The `docker` group is not given to anyone. Published ports bind `127.0.0.1` only, because Docker's own iptables
rules bypass ufw for published ports.

### 5.3 nginx and TLS

- Packages: `nginx`, `certbot`, `python3-certbot-nginx` from Ubuntu. certbot's packaged `certbot.timer` runs
  twice a day; a deploy hook reloads nginx. `gc-certcheck` (weekly timer) runs `certbot renew --dry-run` and
  logs the result.
- One certificate for `groundkrew.com`, `www.groundkrew.com`, `api.groundkrew.com` (HTTP-01 through the webroot).
- Port 80: `/.well-known/acme-challenge/` from the webroot, everything else `301` to `https://$host$request_uri`.
- TLS: `ssl_protocols TLSv1.2 TLSv1.3`, Mozilla intermediate ciphers, `ssl_stapling on`,
  `ssl_stapling_verify on`, resolver `169.254.169.253` (the VPC resolver).
  **Note:** Let's Encrypt has been ending OCSP; certificates issued since mid-2025 may carry no OCSP URL, in
  which case nginx has nothing to staple. The config keeps stapling on (harmless); P6.7 records what the
  issued certificate actually supports.
- `www.groundkrew.com` redirects to `https://groundkrew.com`.
- Headers on every response: `Strict-Transport-Security: max-age=63072000; includeSubDomains` (preload only
  after a month without problems), `X-Content-Type-Options: nosniff`. CSP comes from the app (it needs a
  per-request nonce).
- `api.groundkrew.com`: `/runs/*/events` with `proxy_buffering off`, `proxy_read_timeout 3600s`,
  `proxy_http_version 1.1`, no gzip; everything else 60 s. `client_max_body_size 1m` (web: 2m).
- Rate limits per IP: `auth` 10 requests per minute (burst 10) on `/api/auth/*` and the API's `/auth/*`;
  `api` 20 per second (burst 40); `limit_conn` 10 open streams per IP on the events route. The app adds
  per-user limits behind these (P6.2).
- The default server answers 444 to requests for any other Host (stops scanners hitting the app by IP).

### 5.4 Firewall, SSH, metadata

- **ufw:** default deny incoming, allow outgoing; allow 80/tcp, 443/tcp; allow 22/tcp from the admin CIDR.
  The security group says the same.
- **DOCKER-USER chain** (main daemon, a systemd unit applies it after Docker starts):
  `169.254.169.254` is allowed only from the api and worker static IPs and dropped for every other container.
- **Build user** (`OUTPUT` chain, `-m owner --uid-owner gcbuild`): reject `169.254.0.0/16`, `127.0.0.0/8`
  (except DNS through the resolver below), `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10`,
  the instance's own private and public addresses, and IPv6 equivalents; allow the rest (dependency downloads,
  registry pushes). Rootless Docker's traffic leaves through processes running as `gcbuild`
  (`slirp4netns`/`pasta`, `dockerd`), so these rules cover the containers and the daemon itself.
- **sshd:** `PasswordAuthentication no`, `KbdInteractiveAuthentication no`, `PermitRootLogin no`,
  `AllowUsers ubuntu`, `MaxAuthTries 3`. **fail2ban:** `sshd` jail (5 tries, 1 hour), plus an `nginx-limit-req`
  jail for repeat offenders of the auth zone.
- **unattended-upgrades:** security pocket only, automatic reboot at 04:00 UTC when required (a reboot is
  about a minute of downtime; Compose services and the rootless daemon come back by themselves).
- **Kernel:** `kernel.unprivileged_userns_clone` stays on (rootless Docker needs it); Ubuntu 24.04's AppArmor
  restriction on unprivileged user namespaces gets an AppArmor profile for `rootlesskit` (the one Docker's
  rootless install documents), not a global switch-off.

### 5.5 Secrets

`gc-secrets init` (runs once on the server, root) writes the files in 5.1 with fresh random values:
`SECRETS_KEY` (Fernet), `INTERNAL_JWT_SECRET`, `AUTH_SECRET`, `APPROVAL_TOKEN_KEY`, `POSTGRES_PASSWORD`, and
`DATABASE_URL` built from it. Values you provide are added with `gc-secrets set NAME` (reads from stdin, never
argv, never echoed): `TOKEN_FACTORY_API_KEY`, `TAVILY_API_KEY`, `SMTP_*`, `GITHUB_APP_*`,
`GITHUB_WEBHOOK_SECRET`, `AUTH_GITHUB_*`. `gc-secrets check` lists which are set (names only).

Rotation (each step in `docs/operations.md`, P6.9):

| Secret | Rotation | Effect |
|---|---|---|
| `AUTH_SECRET`, `INTERNAL_JWT_SECRET` | replace, restart web and api | everyone signs in again |
| `APPROVAL_TOKEN_KEY` | replace, restart | approvals already issued but not yet spent must be approved again |
| `SECRETS_KEY` | `gc-secrets rotate-fernet`: MultiFernet re-encrypts every stored secret with the new key, then drops the old one | none (needs a small code change in P6.2: accept `SECRETS_KEY_PREVIOUS`) |
| `POSTGRES_PASSWORD` | `ALTER ROLE`, update both files, restart api and worker | none |
| GitHub App key, webhook secret, OAuth secret | new one in GitHub, `gc-secrets set`, restart, delete the old one in GitHub | none |
| API keys (Token Factory, Tavily, SMTP) | new key at the provider, `gc-secrets set`, restart worker/api | none |

## 6. Build isolation

### 6.1 What it must stop

User code runs in two places: `RUN` steps of a Dockerfile Groundcrew writes or the user owns, and the smoke
container. Either may be hostile. It must not reach: Postgres, the API, the worker, the main Docker daemon,
IMDS (instance role credentials), other builds, or the host's files; and it must not exhaust the host.

### 6.2 Why "rootless + runsc as the default runtime" is not enough on its own

1. `docker build` executes `RUN` steps in BuildKit inside dockerd, with BuildKit's own runc. The daemon's
   `default-runtime` applies to `docker run`, not to build steps. So a runsc default would protect smoke
   containers but not builds.
2. gVisor does not officially support rootless Docker; earlier reports show runsc failing under rootless
   setups, and its cgroup handling needs `--ignore-cgroups` without a delegated cgroup.

### 6.3 Design

- **Daemon:** rootless dockerd as user `gcbuild` (systemd user service with lingering, socket
  `/run/gcbuild/docker.sock`), cgroup v2 delegation enabled for the user (`Delegate=cpu cpuset io memory pids`)
  so limits work rootless. `runsc` installed from gVisor's apt repository, registered as runtime and set as
  `default-runtime`, platform `systrap` (no KVM on t3). BuildKit inside dockerd is not used at all
  (`features.buildkit` irrelevant: the worker never calls `docker build` on this daemon).
- **Per build:** for each run, the worker
  1. creates network `gcb-<run>` (a bridge inside the rootless namespace; nothing else is on it);
  2. starts `moby/buildkit:<pinned>-rootless` as container `gcb-<run>-buildkitd` under runsc, with
     `--cpus 2 --memory 3g --pids-limit 1024`, a 10 GB scratch volume, no host mounts, not privileged, on
     `gcb-<run>`. Inside gVisor, BuildKit runs `RUN` steps with its own runc against gVisor's kernel, never the
     host kernel;
  3. sends the build context to it with `docker buildx build --builder gcb-<run>` (buildx `remote` driver over
     `docker-container://gcb-<run>-buildkitd`), output `type=docker` into the rootless daemon. Registry
     credentials for a push go to the same command through a temporary `DOCKER_CONFIG` that is deleted after
     the push (as today), never written into the daemon or BuildKit's state;
  4. runs the smoke container under runsc on `gcb-<run>` with today's limits (`--cap-drop ALL`,
     `no-new-privileges`, 1 CPU, 512 MB, 256 pids, tmpfs `/tmp`, no published ports) and probes it from a
     second tiny container (`curlimages/curl`, pinned) on the same network: no ports are published on the host
     at all, so `SANDBOX_SMOKE_HOST` goes away for this provider;
  5. pushes (after approval) from the rootless daemon;
  6. cleanup on every path: remove the run's containers, network, BuildKit volume, images, temporary
     `DOCKER_CONFIG`; a sweeper timer removes anything labelled `groundcrew.run` older than 2 hours.
  The BuildKit container lives for the whole run, so the Builder's retries reuse its cache; it goes when the
  run ends.
- **Disk:** `/var/lib/gcbuild` is a 30 GB XFS filesystem in a loop file on the root volume. A build can fill
  its own filesystem, never the root volume Postgres lives on. The per-build scratch volume is capped at
  10 GB with an XFS project quota (falls back to the 30 GB ceiling if quotas do not work rootless; the spike
  checks).
- **Time:** `SANDBOX_BUILD_TIMEOUT_SECONDS` (default 900) and the per-workspace max build time
  (`QUOTA_MAX_BUILD_SECONDS`); on timeout the BuildKit container is killed, not just the CLI.
- **Network:** inside the rootless network namespace, `gcb-<run>` reaches the internet through
  `slirp4netns`/`pasta` with host loopback disabled; on the host, everything leaving as `gcbuild` hits the
  owner-match rules in 5.4, so IMDS, the Docker bridges (where Postgres and the API are), the host's
  addresses and other private ranges are rejected. DNS goes to public resolvers (1.1.1.1, 8.8.8.8) set in the
  rootless daemon's `daemon.json`, not to the VPC resolver.
- **Worker access:** the socket is the only door. Owned by `gcbuild`, mode 0660, group `gcbuild-sock`; only the
  worker container is in that group and only the worker mounts it. A compromised worker gets control of an
  unprivileged daemon whose containers run in gVisor, not root on the host (that is the point of rootless).
  Mutual TLS is not needed for a local socket.
- **Code changes (P6.5):** `SANDBOX_PROVIDER=isolated` in `app/sandbox/` next to `docker`: same `Sandbox`
  protocol, same `BuildResult` parsing (BuildKit's plain output is the same), the per-run builder lifecycle,
  probe-container smoke check, and the cleanup above. Local dev keeps `docker` with dind.

### 6.4 Tests and live checks

- Unit (CI): the exact `docker` arguments for each step (runtime runsc, no `--privileged`, no `-v`/`--mount`
  of host paths, no `-p`, limits present, labels, cleanup order), and that the provider refuses to start when
  `docker info` on the socket does not report rootless and runsc as default runtime.
- Live (`make build-isolation-check` on the server, P6.7, and on a local Ubuntu VM during the spike): a
  Dockerfile whose `RUN` steps and smoke container try, each with a 3 s timeout:
  `169.254.169.254` (IMDS), the Postgres container IP on 5432, the API on its container IP and on
  `127.0.0.1:8000`, the host's private IP, the main Docker socket path; plus a fork bomb (pids limit), a 4 GB
  allocation (memory limit), filling the disk (scratch limit), `sleep 9999` (timeout); and one that must
  succeed: `pip download requests` (egress works). The check passes only if every forbidden one fails and
  egress works. Results go in `docs/phase6-progress.md`.

### 6.5 Spike (first task of P6.5, on a throwaway local Ubuntu 24.04 VM, not AWS)

Questions with yes/no answers: does `runsc` run as `default-runtime` under rootless dockerd with cgroup
delegation (with or without `--ignore-cgroups`, and do `--memory`/`--pids-limit` still apply)? does
`moby/buildkit:rootless` build a Python and a Node app inside runsc? does the owner-match firewall catch
rootless traffic (`pasta` vs `slirp4netns`)? is build time acceptable (target: under 1.5x today's dind time)?

### 6.6 Fallbacks if the spike says no (decision 6.2)

| Option | What changes | Protects against | Gives up |
|---|---|---|---|
| **A. Rootless, gVisor for smoke only, BuildKit rootless** (recommended fallback) | smoke and probe containers under runsc if that part works; builds in `moby/buildkit:rootless` under runc in the rootless daemon | host root (rootless + user namespaces + seccomp), IMDS and internal services (firewall) | a kernel boundary for build steps: a kernel exploit from a `RUN` step reaches the `gcbuild` user |
| B. Rootful second daemon, gVisor for everything | a separate dockerd as root with `userns-remap`, runsc default, BuildKit inside runsc as above | kernel exploits from user code (gVisor) | a compromised worker controls a root daemon (host root), mitigated only by the socket ACL |
| C. Rootless, runc everywhere | no gVisor | as A, minus gVisor for smoke | the kernel boundary everywhere |

I would take A over B: on a single host that also holds the database, a compromised worker becoming host root
is the worse failure, and the worker parses untrusted repositories.

## 7. Releases

- **CI** (`.github/workflows/release.yml`, on tags `v*` and manual dispatch): build `api` and `web` images
  (amd64), Trivy scan (`--severity CRITICAL --exit-code 1`, decision 6.5 on `--ignore-unfixed`), push to
  `ghcr.io/groundscrew-ai/groundcrew-api:<tag>` and `-web:<tag>`, sign both with cosign keyless (GitHub OIDC),
  then create a GitHub Release whose asset `release.json` lists each image **by digest** plus the git SHA and
  whether it has migrations. Only `permissions: packages: write, contents: write, id-token: write`; no AWS
  keys, no SSH.
- **Immutable:** the server only ever pulls `image@sha256:...` from `release.json`, so a re-pushed tag
  changes nothing. CI refuses to push a tag that already exists in GHCR.
- **Updater** (`gc-update`, systemd timer every 5 minutes, root):
  1. read the latest non-prerelease release from the GitHub API (public, no token; 12 calls an hour);
  2. if newer than `current.json`: download `release.json`, verify the cosign signatures against the repo's
     workflow identity, `docker pull` by digest;
  3. run `migrate` with the new api image (migrations must be additive: expand, deploy, then contract in a
     later release; the updater never downgrades the database);
  4. replace `worker` (SIGTERM, 30 s grace; its job is requeued), wait for its heartbeat; then `api`, wait for
     `/ready`; then `web`, wait for `/api/health`; then a smoke over nginx (`https://api.groundkrew.com/ready`,
     `https://groundkrew.com/`);
  5. on any failure: put back the digests from `previous.json` in the same order, record the failure in
     `/var/log/groundcrew/updater.log` and the journal, and mark that release as bad so it is not retried.
  Each restart is a few seconds of downtime for that one service (no second replica on one host).
- **Manual:** `gc-deploy v1.4.0` (same steps for a named tag), `gc-rollback` (to `previous.json`),
  `gc-pause-updates` / `gc-resume-updates`, `gc-status` (running digests, last check, last failure).
- **Host scripts** are part of the release too: `release.json` lists the tag; `gc-update` refreshes
  `/opt/groundcrew/` from the tagged `infra/platform/host/` tarball attached to the release, so script and
  Compose changes ship the same way.

## 8. Quotas and abuse settings (defaults; all configurable)

| Setting | Default | Where |
|---|---|---|
| `LLM_WORKSPACE_DAILY_USD_CAP` | $0.50 | per workspace (override in `workspaces.settings` by an admin) |
| `LLM_DAILY_USD_CAP` (global) | $5 | env |
| `LLM_TOTAL_USD_CAP` (global) | your credit balance minus a margin | env |
| `QUOTA_TAVILY_PER_WORKSPACE_DAY` | 10 | |
| `QUOTA_CONCURRENT_RUNS_PER_WORKSPACE` | 2 | queued runs wait, they are not refused |
| `QUOTA_BUILDS_PER_WORKSPACE_DAY` | 10 | |
| `QUOTA_MAX_BUILD_SECONDS` | 900 | |
| `QUOTA_CONNECTIONS_PER_WORKSPACE` | 10 | |
| `SIGNUPS_PER_HOUR` (global), `SIGNUPS_PER_IP_DAY` | 30, 5 | |
| Signup switch | open | `platform_settings` row; `gc-admin signups pause|resume` on the server, and a toggle in Settings for users listed in `PLATFORM_ADMINS` (emails) |
| Per-user API limit | 120 requests a minute, 10 stream tokens a minute | in-process limiter (one API host) |

Each limit returns a clear message naming the limit and when it resets (`429` with `Retry-After` for rate
limits, `409 quota_exceeded` with the quota name for quotas), and Settings, Usage shows today's numbers.

## 9. Dev-only paths in production (P6.2 tests)

| Path | Production behaviour | Test |
|---|---|---|
| `make kind-register` / `require_dev` | refuses unless `APP_ENV=dev` and local API | exists; add the production case |
| Self-approve (`LIVE_SCRIPT_SELF_APPROVE`) | route not mounted; setting refused at start-up | new |
| `GET /workspaces/{id}/evidence` | route not mounted (404) | new |
| Web test auth (`E2E_TEST_AUTH`) | provider not registered; web refuses to start with it set and `NODE_ENV=production` | new (vitest) |
| Deploy fixtures (`{"fixture": ...}`) | 422 | exists; add the production case |
| Local kind target and private API servers (`K8S_ALLOW_PRIVATE_SERVERS`) | refused: kubeconfig with a private or loopback server is rejected | new |
| Pipeline fake (`PIPELINE_FAKE`) | setting refused at start-up | new |

## 10. What you do by hand (and when)

| When | Task | Detail |
|---|---|---|
| Now (P6.1 review) | Answer decisions 6.1 to 6.6 (section 11) | |
| Before P6.4 | Your admin IP | a /32 for SSH (or tell me to use only nginx and no SSH ingress) |
| Before P6.4 | An SSH public key for the `ubuntu` user | ed25519 public key text |
| Before P6.4 | Credentials for Terraform | which local profile or SSO session I may use to run `terraform plan` (read-only) and, after your OK, `apply`. It needs EC2, IAM role, and the state bucket |
| P6.3 | SMTP | Resend: add the domain, add its DNS records, create an SMTP key. Gmail: an app password on the account that will send |
| P6.3 | DNS for mail | SPF, DKIM, DMARC records (exact values in P6.3 once the provider is known; DMARC starts at `p=none` with reports to an address you choose, then `quarantine`) |
| P6.3 | Production GitHub App | create it with the settings I list in P6.3 (public, callback `https://groundkrew.com/api/auth/callback/github`, setup URL, webhook `https://api.groundkrew.com/github/webhook` with a secret, the same permissions as the dev app), generate a private key, send me nothing: you paste values into `gc-secrets set` on the server |
| P6.4 | OK to apply Terraform | after reading the plan |
| P6.6 | GitHub repo settings | allow Actions to create releases and write packages; make the two GHCR packages public (decision 6.3) |
| P6.7 | DNS A records | at the registrar, TTL 300: `groundkrew.com` A `<EIP>`, `www.groundkrew.com` A `<EIP>`, `api.groundkrew.com` A `<EIP>`. Optional and recommended: `groundkrew.com` CAA `0 issue "letsencrypt.org"`. No AAAA records. Remove any existing A/AAAA/CNAME for those names (parking pages) |
| P6.7 | Production keys on the server | `TOKEN_FACTORY_API_KEY`, `TAVILY_API_KEY` (ideally separate from dev keys, so their usage and caps are separate), through `gc-secrets set` over SSH |
| P6.8 | Move existing AWS connections | update each `groundcrew-read-only` stack's `GroundcrewPrincipalArn` to `arn:aws:iam::881050425883:role/groundcrew-platform` (CloudFormation Update, use current template, change one parameter), then Verify in Connections. Then delete the IAM user's access keys and the user |
| P6.8 | End-to-end sign-offs | sign in with GitHub and email, approve the deploy and the incident fix yourself |
| P6.9 | Legal text | read the Terms of Service and Privacy Policy I draft and have them checked; they are product text, not legal advice |
| Any time | A contact address | for the Security and Privacy pages (for example `security@groundkrew.com`, which needs a mailbox or forwarding at your registrar) |

## 11. Decisions for you

| # | Question | Recommendation |
|---|---|---|
| 6.1 | Email and password accounts and Google sign-in exist today. The brief asks for GitHub and magic links. | Production offers **GitHub and magic links only** (`PASSWORD_AUTH_ENABLED=false`, Google unset). The code stays; existing password users can sign in by magic link to the same verified address |
| 6.2 | Build isolation fallback if the spike fails | **A** (section 6.6): rootless everywhere, gVisor where it works. You hear the spike result before P6.5 code is written |
| 6.3 | GHCR images public or private | **Public** (the repository is public; no pull token on the server). Private needs a read-only token in `/etc/groundcrew/` |
| 6.4 | Terraform state | Use the existing state bucket in 881050425883 with key `groundcrew/platform.tfstate` and its lock (I find its name read-only in P6.4 and show you). The brief's "no S3" is about the running platform; say if you want local state instead (then the state file lives only on your machine) |
| 6.5 | Trivy on CRITICAL: also fail on vulnerabilities that have no fix yet? | `--ignore-unfixed`: a critical with no fixed version cannot be acted on and would block every release; they are listed in the job summary |
| 6.6 | Instance | `t3.large` unlimited. If the spike shows builds under gVisor are much slower, `t3.xlarge` (about $61 more a month) |

## 12. Order and size of the remaining milestones

| Milestone | Main files | Depends on |
|---|---|---|
| P6.2 | `app/config.py`, `app/routes/*`, new `app/quotas.py`, `app/audit.py`, migrations (roles, audit_events, platform_settings, quotas usage), `apps/web/middleware.ts`, public pages, tests (`test_tenant_isolation.py` walks every route in the OpenAPI schema) | decisions 6.1 |
| P6.3 | magic links (API + Auth.js Email provider through our API), webhook route, connect page | P6.2 |
| P6.4 | `infra/platform/*.tf` | your IP, key, credentials, decision 6.4 |
| P6.5 | spike, then `infra/platform/host/` (cloud-init, `gc-*` scripts, compose, nginx, ufw, fail2ban, rootless daemon), `app/sandbox/isolated.py` | P6.4 for the real host; the spike runs locally first |
| P6.6 | `release.yml`, `apps/web/Dockerfile`, api image hardening, `gc-update` | P6.5 |
| P6.7 | first release, DNS, TLS, smoke, isolation check | DNS records |
| P6.8 | live end-to-end | your sign-offs |
| P6.9 | `docs/operations.md`, final report | |
