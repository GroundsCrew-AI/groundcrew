# Phase 6 progress: groundkrew.com

Brief: `docs/prompts/phase6.md`. Plan: `docs/phase6-plan.md`. Re-read both after a context compaction.

## Status

| Milestone | State | PR |
|---|---|---|
| P6.1 Plan | done 2026-10-21 | draft PR #56 (`phase6-plan`) |
| P6.2 Code hardening | done 2026-10-21 | draft PR #57 (`phase6-hardening`, stacked on #56) |
| P6.3 Email sign-in, production GitHub App, AWS connect by upload | done 2026-10-22 | draft PR #58 (`phase6-email-github`, stacked on #57) |
| P6.4 Terraform | applied 2026-10-22 | draft PR #60 (`phase6-terraform`, stacked on #58) |
| P6.5 Host setup | done 2026-10-22, waiting for review | draft PR (`phase6-host`, stacked on #60) |
| P6.6 Release pipeline | not started | |
| P6.7 First production deploy | not started | |
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

## Cost

| Item | Monthly |
|---|---|
| Planned run rate (t3.large unlimited, 80 GiB gp3, Elastic IP) | about $71, up to about $81 with many builds |
| Spent so far in Phase 6 | the platform host runs from 2026-10-22: about $2.35 a day (instance, volume, IPv4) |

## How to resume

1. Read `docs/prompts/phase6.md`, this file, and `docs/phase6-plan.md`.
2. Check the open draft PR for the current milestone and any review comments.
3. No AWS write, Terraform apply, DNS, GitHub settings or server change without an explicit OK in the chat.
   Check the account is 881050425883 and the region us-east-2 before any AWS call.
