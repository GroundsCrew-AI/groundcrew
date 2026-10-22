# Phase 6 progress: groundkrew.com

Brief: `docs/prompts/phase6.md`. Plan: `docs/phase6-plan.md`. Re-read both after a context compaction.

## Status

| Milestone | State | PR |
|---|---|---|
| P6.1 Plan | done 2026-10-21 | draft PR #56 (`phase6-plan`) |
| P6.2 Code hardening | done 2026-10-21 | draft PR #57 (`phase6-hardening`, stacked on #56) |
| P6.3 Email sign-in, production GitHub App, AWS connect by upload | done 2026-10-22 | draft PR #58 (`phase6-email-github`, stacked on #57) |
| P6.4 Terraform | applied 2026-10-22 | draft PR #60 (`phase6-terraform`, stacked on #58) |
| P6.5 Host setup | done 2026-10-22 | draft PR #61 (`phase6-host`, stacked on #60) |
| P6.6 Release pipeline | skipped on 2026-10-22 (no GitHub Actions or GHCR for now): manual deploy from source instead | |
| P6.7 First production deploy | done 2026-10-22: https://groundkrew.com live | draft PR (`phase6-deploy`, stacked on #61) |
| P6.8 End-to-end on production | not started | |
| P6.9 Runbook, final report | not started | |

## P6.1 Plan (2026-10-21)

Done:
- Brief saved as `docs/prompts/phase6.md`.
- `docs/phase6-plan.md`: instance size and cost, Terraform layout, host layout, build isolation design with
  a spike and fallbacks, releases, quotas, dev-path lockout, everything you do by hand, decisions.
- No AWS calls, no changes outside the repo.

Findings that shape the plan:
- gVisor does not officially support rootless Docker, and `docker build` runs `RUN` steps with BuildKit's
  own runc regardless of the daemon's default runtime. The design runs BuildKit inside a gVisor container
  per build; a spike on a local Ubuntu 24.04 VM confirms it before P6.5 code.
- Much of the account work already exists (GitHub/Google/password accounts, invites, workspace and global
  LLM caps, sign-in rate limits, production start-up checks). P6.2 extends it rather than rebuilding it.
- No GitHub webhook receiver exists yet (P6.3).
- No production web image exists (only `Dockerfile.dev`; P6.6).

Decisions (2026-10-21, plan section 11): GitHub and magic links only in production; build daemon always
rootless, gVisor where it works; private GHCR with a read-only pull token until the repository is public;
existing state bucket, key `groundcrew/platform.tfstate`; Trivy fails on fixable criticals, HIGH as warnings;
`t3.large`.

Open issues:
- Let's Encrypt is ending OCSP; stapling may have nothing to staple. Checked in P6.7.
- The state bucket's name is found read-only in P6.4.

## P6.2 Code hardening (2026-10-21)

Done (one commit each, branch `phase6-hardening`):
- **Production lockout.** Staging and production refuse to start with `LIVE_SCRIPT_SELF_APPROVE`,
  `PIPELINE_FAKE`, `K8S_ALLOW_PRIVATE_SERVERS` or `DEPLOY_FIXTURES_DIR` set; kind-register, self-approve,
  the dev evidence endpoint, deploy fixtures, the e2e test user, private cluster addresses and the
  local kind target each refuse where used (`tests/test_production_lockout.py`, web
  `lib/test-auth.test.ts`). Password and Google sign-in are off outside dev and test unless
  `PASSWORD_AUTH_ENABLED` / `GOOGLE_AUTH_ENABLED` (decision 6.1): endpoints answer 403, the web app hides
  them and sends the password pages to sign-in. Tests now run with `APP_ENV=test`.
- **Roles and audit log.** `owner` / `approver` / `viewer`; the migration turns `member` into `approver`
  (what members could do before), new members default to `viewer`. Viewers get 403 on every change and
  on approvals (checked in the routes and in `decide_approval`). `emergency_approvers` is
  `owners | approvers`. `audit_events` records approvals, member/invite/workspace/connection changes,
  deploys, reported incidents, stopped runs, platform switches, with the client address; Settings,
  Activity lists it.
- **Tenant isolation.** `tests/test_tenant_isolation.py` calls every route in the OpenAPI schema as a
  member of another workspace with real ids and expects 404 with nothing changed; a new route fails the
  test until it is listed.
- **Quotas.** Tavily calls and builds per workspace per day (atomic counter), runs a person started going
  at once, connections, max build time; a per-workspace override only an operator can set; 429
  `quota_exceeded` with the limit and reset time; Settings, Usage. Spend-limit messages are plain words;
  an owner can lower the workspace's daily cap, never raise it.
- **Abuse protection.** Per-user API calls, stream tokens and open streams; 1 MB request bodies (API and
  web proxy); sign-ups per hour and per address; `platform_settings.signups_paused` (invited people
  still join), switched with `python -m app.admin_cli` or in Settings for `PLATFORM_ADMINS`.
- **Headers, cookies, CSRF, CORS.** Per-request CSP nonce, frame-ancestors none, HSTS over HTTPS,
  Referrer-Policy, Permissions-Policy, COOP; `__Secure-` HttpOnly SameSite=Lax cookies over HTTPS; the
  proxy refuses cross-origin writes; the API sends deny-all headers, CORS only for `WEB_ORIGIN` reads,
  and no `/docs` or `/openapi.json` outside dev and test. Checked headless in Chromium on the dev stack:
  every script carries the nonce, no CSP errors.
- **Deletion and access.** Delete a connection (credential, resources, edges, events), a workspace (typed
  name; returns what to remove on the customer's side), or your account (preview; blocked while you are
  the only owner of a shared workspace). Settings shows what Groundcrew can access per connection.
- **Public pages** (built by a parallel agent, reviewed and wired here): landing at `/` for signed-out
  visitors, `/terms`, `/privacy`, `/security`, `/docs` with AWS (template upload), GitHub, Vercel and
  Kubernetes guides; `aws-connect.yaml` and the two Kubernetes RBAC files are served from the site, with a
  test that they match `infra/`.
- Fixed on the way: #54 had dropped load balancers and Aurora clusters from the watcher (NameError on any
  ALB); restored in its own commit.

Tests: API 1109 passed, 6 skipped (ruff, mypy clean); web 252 passed (tsc, eslint clean); Playwright e2e
5 passed against a production build (`scripts/e2e.sh`, run on a copy of apps/web so the dev stack's `.next`
was untouched), including sign-in, the live run stream and a deploy approval under the new CSP.
GitHub Actions did not run on the PR: the organization's billing refused to start the jobs.

Teammate areas touched (Ahmed): `app/agents/medic/agent.py` (research quota), `app/incidents/watcher.py`
(the #54 regression).

Decisions made while building (tell me if any should change):
- Runs over the concurrency quota are refused with a 429 that says so, not queued (simpler and clearer;
  the watcher's own investigations are never held back).
- Open-stream and rate-limit counts live in the API process (one API host). They reset when it restarts.
- The interactive API docs are not served in production.

Open issues for you:
- Legal text in `/terms` and `/privacy` is a draft and needs a legal review. Choices made there: minimum
  age 16, liability capped at fees paid or USD 50, "we do not use your data to train models" (check
  Nebius Token Factory's data terms before standing behind it).
- Contact addresses `security@groundkrew.com` and `privacy@groundkrew.com` are placeholders
  (`apps/web/components/public/contact.ts`); they need mailboxes or forwarding.
- The Kubernetes guide creates a 24-hour ServiceAccount token, so users paste a new kubeconfig daily; a
  long-lived token Secret would avoid that. Your call.
- The landing and policy pages say the server runs in us-east-2 (true once P6.7 is done).

## P6.3 Email sign-in, GitHub App, AWS connect by upload (2026-10-22)

Done (branch `phase6-email-github`):
- **Email sign-in links.** `POST /auth/email-link` (same answer for every address; while sign-ups are
  paused only existing or invited addresses get a link), `POST /auth/email-link/verify` (single use,
  15 minutes, a newer link retires older ones; creates the account and workspace on first use after the
  sign-up checks; takes over an unproven password account). The link opens `/signin/email`, where a
  button signs in, so mail scanners cannot use it up. Production refuses to start without `SMTP_HOST`
  while it is on. Checked live on the dev stack in headless Chromium: link sent, signed in, account and
  workspace made, second use refused, no console errors.
- **GitHub, multi-tenant.** Found and fixed: the installation picker listed every installation of the
  app (other customers' repository names) and linking accepted any of them. Now a workspace uses only
  installations an owner proved access to through GitHub OAuth (`GET /user/installations` with a
  one-time token that is not kept; state bound to user and workspace for 10 minutes), plus those its
  connections already use. Existing connections keep working.
- **GitHub webhooks.** `POST /github/webhook`, HMAC-SHA256 checked, 503 without a secret; uninstall,
  suspend/unsuspend, and removed repositories keep connections and claims in step.
- **Production GitHub App settings** listed in `docs/self-hosting.md` (callback URLs, Setup URL,
  webhook, the exact permissions).
- **AWS connect by template upload.** The Connect AWS dialog: download `aws-connect.yaml` from the site,
  open CloudFormation, Upload a template file, stack name and parameters with copy buttons, paste the
  RoleArn, Verify (unchanged). The CLI command stays as an alternative. Enable deploys / emergency fixes
  already fall back to Upload a template file when no S3 URL is set.
- **Email DNS** (SPF, DKIM, DMARC) for Resend documented in `docs/self-hosting.md`.

Tests: API 1122 passed, 6 skipped; web 252 passed; Playwright e2e 5 passed on a production build. The
tenant isolation suite covers the new routes (the webhook is listed as unscoped and tested on its own).

Decisions made while building (tell me if any should change):
- GitHub callback URLs are on `groundkrew.com`, not `api.groundkrew.com` as the brief said: sign-in is
  handled by the web app (Auth.js), whose session cookie lives there. The webhook is on the API.
- "Request user authorization (OAuth) during installation" stays off; the Setup URL starts the same
  checked authorization instead.
- No Workflows permission: a fix PR that edits `.github/workflows/` would be refused by GitHub.

Open issues for you:
- Pick the email provider (Resend recommended; Gmail cannot sign mail as groundkrew.com).
- Create the production GitHub App with the settings in `docs/self-hosting.md` when the server exists.

## P6.4 Terraform (2026-10-22)

Done:
- `infra/platform/` (see its README). Read-only checks before the plan: account 881050425883 through
  the `groundcrew-demo` profile, no existing `groundcrew-platform` role or instance profile, no state at
  the key, no Elastic IPs, t3.large in every zone.
- Plan reviewed (10 to add, 0 change, 0 destroy), applied after the OK in chat on 2026-10-22:

| | |
| --- | --- |
| Instance | `i-0c134cf1e7025c3a8`, t3.large, us-east-2a, Ubuntu 24.04 (`ami-02ad619be5bc165df`) |
| Elastic IP | **3.19.1.100** (`eipalloc-033ed459504e18ff3`) |
| Security group | `sg-0ac60553eac39b18b`: 80, 443 from anywhere; 22 from 119.73.108.213/32 |
| Role | `arn:aws:iam::881050425883:role/groundcrew-platform` (instance profile of the same name) |
| State | `s3://groundcrew-checkout-tfstate-881050425883/groundcrew/platform.tfstate` |

- `infra/aws-connect.yaml` defaults `GroundcrewPrincipalArn` to the role; `docs/self-hosting.md` says how
  existing stacks move over. `tests/test_platform_terraform.py` pins the security settings.

Checked on the host after first boot (SSH with ~/.ssh/groundkrew): Ubuntu 24.04.5, ufw active
(80, 443 anywhere; 22 from 119.73.108.213 only), sshd key-only with no root login and
`AllowUsers ubuntu`, fail2ban's sshd jail on, unattended-upgrades on with 04:00 reboots, 4 GiB swap,
77 GB root, only sshd listening publicly; IMDSv1 refused (401), IMDSv2 gives the
`groundcrew-platform` role. From outside only 22 answers.

DNS records to add at the registrar (TTL 300): `groundkrew.com A 3.19.1.100`,
`www.groundkrew.com A 3.19.1.100`, `api.groundkrew.com A 3.19.1.100`, and recommended
`groundkrew.com CAA 0 issue "letsencrypt.org"`.

## P6.5 Host setup (2026-10-22)

Done:
- **Spike on the host** (results in plan section 6.3a): gVisor under the rootless daemon works with
  `--ignore-cgroups`; per-container limits are replaced by limits on the build user's slice (memory bomb
  and fork bomb held, host unaffected); BuildKit builds inside gVisor, unprivileged; the owner-match
  firewall blocks the metadata service and every internal address while the internet works.
- **`infra/platform/host/`**: `install.sh` with steps 10 to 50, `push.sh`, the production `compose.yml`,
  nginx configs, the `gc-*` commands. Ran on the host twice (the second run changed nothing). Checked:
  30 GB XFS build filesystem, slice limits, gcbuild uid/gid 1500, firewall chains, nginx dropping bare-IP
  requests and serving only ACME challenges until the certificate, fail2ban (sshd, nginx-limit-req),
  timers (certbot, build sweeper), journald retention, only 22 and 80 listening publicly.
- **`gc-isolation-check` on the host: all pass** (metadata service, host private and public addresses,
  loopback, Docker bridge, a stand-in internal service: unreachable from a build; internet reachable;
  gVisor kernel; a non-API container on the main daemon cannot reach the metadata service). The run
  against the real Postgres and API comes in P6.7.
- **Code**: `SANDBOX_PROVIDER=isolated` (`app/sandbox/isolated.py`), the worker heartbeat for its health
  check. Tests: `tests/test_sandbox_isolated.py`, `tests/test_platform_host.py`.
- Tests: API 1157 passed, 6 skipped (a token test that expired during the now longer suite was made
  to build its tokens when it runs).
- Hardening beyond the plan: `api.groundkrew.com` serves only the run stream, the GitHub webhook,
  `/health` and `/ready`; all other API calls go through the web app's proxy inside the host.

Open issues:
- The Compose services start in P6.6/P6.7, once the release pipeline publishes the images.
- `gc-certs issue` accepts the Let's Encrypt Subscriber Agreement only with `--agree-tos`, which needs
  your OK in P6.7.

## Change of plan (2026-10-22): manual deploys from source, then P6.7

P6.6 (GitHub Actions, GHCR, pull-based updater) is skipped for now. Instead:
- `infra/platform/bin/deploy <ref>` / `rollback` (our wrappers in `ops/bin/`, git-ignored): `git archive`
  over SSH, `gc-release` builds the images on the host, migrates, rolls out worker, api, web with health
  checks, checks HTTPS, and rolls back on failure; 3 releases kept; going back runs no migrations.
- Production web image (`apps/web/Dockerfile`, standalone, uid 10001) and `/api/health`.
- SSH is a list of admin addresses (Terraform `admin_cidrs`, ufw follows through `push.sh`).

## P6.7 First production deploy (2026-10-22)

Steps, each OK'd in chat:
1. Security group: SSH also from the new admin address (Terraform, 1 added, 1 changed in place). ufw on
   the host still allowed only the old one; recovered by stop, a one-line `bootcmd` user data, start
   (the host came back with everything running). The original user data is saved; put it back at the
   next planned stop.
2. Secrets on the host: `SMTP_PASSWORD` (set by you), `TOKEN_FACTORY_API_KEY` and `TAVILY_API_KEY` (piped
   from the local `.env`, never printed).
3. Certificates: `gc-certs issue` for groundkrew.com, www, api (Let's Encrypt, expires 2027-01-19,
   renewed by certbot's timer, weekly dry run). Fixed on the way: Ubuntu's own `ssl_protocols` made the
   site a duplicate; TLS settings now live at http level, so TLS 1.1 is refused on every server; a
   default HTTPS server refuses the handshake for other names; `/signin` no longer redirects; no duplicate
   HSTS. OCSP stapling: the certificate has no OCSP URL (Let's Encrypt dropped OCSP), so there is nothing
   to staple; the setting stays for a certificate that has one.
4. Deploy `13196e0a74bc`: all migrations on a fresh database, worker, api, web healthy, HTTPS checks
   passed.

Checks over HTTPS from outside: landing, sign-in, Terms 200; `/aws-connect.yaml` 200; private pages
redirect to sign-in; `/api/gc/*` 401 without a session, 403 for a cross-origin write; on
api.groundkrew.com only `/ready`, `/health`, the run stream (401 with a bad token) and the webhook (503
until its secret is set) answer, everything else 404; HTTP and www redirect to https://groundkrew.com;
TLS 1.2 and 1.3 only; CSP with nonce, HSTS, X-Frame-Options DENY, nosniff, Referrer-Policy,
Permissions-Policy, COOP; cookies `__Host-`/`__Secure-`, HttpOnly, Secure, SameSite=Lax.

On the host: `gc-isolation-check` passes against the real Postgres (172.30.0.2:5432) and API
(172.30.0.4:8000) as well as the metadata service, host addresses, loopback, and the Docker bridge; the
internet works from builds; builds run on gVisor's kernel. The worker's AWS identity is
`assumed-role/groundcrew-platform` (no keys); the web container cannot reach the metadata service. All
app containers run as uid 10001, Postgres as 999; Postgres has no published port.

Later the same day:
- Production GitHub App `groundcrew-app` (GroundsCrew-AI) created by you with the documented settings;
  its id, client id and secret, webhook secret, and private key set with `gc-secrets`. The server
  authenticates as the app (checked: permissions actions read, contents write, emails read, metadata
  read, pull requests write). "Continue with GitHub" is on the sign-in page; unsigned webhook
  deliveries get 401.
- Release `14423f78caf4` (Groundcrew favicon, SVG icon, Apple touch icon) deployed; `13196e0a74bc` kept
  for rollback.

UI pass (2026-10-22, on `phase6-deploy`): release `57f18be6afa5` live, `14423f78caf4` kept for rollback.
All public pages and docs return 200 on https://groundkrew.com, no console errors, no horizontal scroll at 375 px.
- Landing page rebuilt: hero with an illustrated incident and approval, works-with strip, four
  feature sections with drawn mockups, safety grid, steps, agents, FAQ, closing call to action.
  Sticky public header with a phone menu; footer with Product, Docs, Trust links.
- Docs: sidebar layout, "On this page", Previous/Next; new pages getting-started, discovery, deploys,
  incidents, notifications, roles, approvals, limits, data, faq; connect guides expanded with
  troubleshooting. Kubernetes guide now links the RBAC files in the repository (those links work once
  the repository is public).
- App: connection actions wrap on phones (were clipped), two-column overview stats on phones,
  operator wording removed from the Kubernetes and GitHub connect dialogs.
- Checked: tsc, eslint, vitest (252), Playwright e2e (5) plus signed-in screenshots at 1440 and 390 px
  with no horizontal scroll, public pages at 360 px, production web image serves every public page.
- Unconfirmed in the docs: Slack error names in notifications troubleshooting, and the limits page
  pointing people to the Security page contact to ask for higher limits.

Deploy fix (2026-10-22): release `5aa49f3d7482` live, `57f18be6afa5` kept for rollback.
- Deploys failed at the first clone with EACCES on /scratch: a named volume Docker created owned by
  root, while the worker runs as uid 10001 on a read-only root filesystem.
- /scratch is now a tmpfs owned by the service uid (worker 512m, api 64m), TMPDIR and HOME point at
  it, worker mem_limit 1280m. API and worker refuse to start when TMPDIR is not writable.
- Checked on the server: worker /scratch is tmpfs 10001:10001 0700, a clone of
  GroundsCrew-AI/legacy-flask-app lands there. A deploy from the Deploy page still to be run.
- The unused `groundcrew_worker-scratch` volume is still on the server (remove later).

Build, Medic, and setup fixes (2026-10-22): release `e111b5269f33` live, `57f18be6afa5` kept.
- Builds: the rootless daemon's socket had a sub-gid group (300987) the worker is not in; daemon.json
  sets "group": "root" (gcbuild 1500 on the host). Step 20 restarts the worker after the daemon, since
  its bind mount pins the old socket. gc-isolation-check now checks the worker reaches the daemon (ok).
- Builder stops at once when the build service is down. Medic rolls back only to revisions whose ECR
  images and secrets still exist, names the stack parameter on AccessDenied, shows giving up once.
- Emergency-fix setup: values from discovery (GET /connections/{id}/aws/operate-values), Verify refuses
  a role that reaches no discovered service. Connect dialogs restyled as titled steps.
- Limits raised in ops/production.env (Tavily 50/day, builds 30/day, 4 runs at once, 25 connections,
  $5 per workspace a day, $30 platform a day, $0.60 a run). LLM_TOTAL_USD_CAP still $25.
- Checkout environment (outside Groundcrew): rebuilt by Terraform with empty ECR repositories and DB;
  images for af3758a pushed, migrations run, services back on revision 31. The checkout-demo repository
  variables ECS_SUBNETS and ECS_TASK_SECURITY_GROUP still name the old VPC.

Medic diagnosis grounding (2026-10-22, not deployed yet): from the legacy-flask-app incident,
where Medic blamed task definition :3 with high confidence for gunicorn "no URI read" worker
timeouts although the worker settings had not changed, and never read why the service was unhealthy.
- ECS reader (`app/telemetry/ecs.py`) now also reads the running tasks' health, the service's
  load balancer target groups (health check path, port, matcher, and why each target fails),
  and compares each deploy's task definition with the one before it: image, command,
  environment and secret names (never values), limits. A deploy with the same images and
  commands is marked as shipping no new application code. No new permissions: the connect
  template already allows DescribeTaskDefinition, DescribeTargetGroups, DescribeTargetHealth.
- Diagnosis rules: a deploy before the errors is not proof; timing alone or contradicted
  evidence caps confidence at medium; failing health checks name the symptom; idle-connection
  worker timeouts are not "every request hangs". New ranking weights for health and deploy diffs.
- When Medic cannot fix it, the outcome adds the diagnosis's manual fix ("To fix it by hand:
  ..."), and notes read as sentences (no more "revision.; No fix PR"). "no longer exist" for
  several missing items.
- Teammate code touched (Phase 5, for review): app/agents/medic/agent.py*, diagnose.py*,
  evidence.py*, app/telemetry/ecs.py*.

Open issues:
- The automatic rollback path has unit-level logic only; it has not been exercised live (a deploy of a
  commit that fails its health check would show it).
- Restore the instance's original user data at the next stop (saved in the session scratchpad and in
  Terraform: `terraform apply -replace` would also rebuild it).

## Cost

| Item | Monthly |
|---|---|
| Planned run rate (t3.large unlimited, 80 GiB gp3, Elastic IP) | about $71, up to about $81 with many builds |
| Spent so far in Phase 6 | the platform host runs from 2026-10-22: about $2.35 a day (instance, volume, IPv4); model calls bounded by the caps |

## How to resume

1. Read `docs/prompts/phase6.md`, this file, and `docs/phase6-plan.md`.
2. Check the open draft PR for the current milestone and any review comments.
3. No AWS write, Terraform apply, DNS, GitHub settings or server change without an explicit OK in the chat.
   Check the account is 881050425883 and the region us-east-2 before any AWS call.
