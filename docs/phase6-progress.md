# Phase 6 progress: groundkrew.com

Brief: `docs/prompts/phase6.md`. Plan: `docs/phase6-plan.md`. Re-read both after a context compaction.

## Status

| Milestone | State | PR |
|---|---|---|
| P6.1 Plan | done 2026-10-21 | draft PR #56 (`phase6-plan`) |
| P6.2 Code hardening | done 2026-10-21, waiting for review | draft PR (`phase6-hardening`, stacked on #56) |
| P6.3 Email sign-in, production GitHub App, AWS connect by upload | not started | |
| P6.4 Terraform | not started | |
| P6.5 Host setup | not started (starts with the build isolation spike, plan section 6.5) | |
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

## Cost

| Item | Monthly |
|---|---|
| Planned run rate (t3.large unlimited, 80 GiB gp3, Elastic IP) | about $71, up to about $81 with many builds |
| Spent so far in Phase 6 | $0 |

## How to resume

1. Read `docs/prompts/phase6.md`, this file, and `docs/phase6-plan.md`.
2. Check the open draft PR for the current milestone and any review comments.
3. No AWS write, Terraform apply, DNS, GitHub settings or server change without an explicit OK in the chat.
   Check the account is 881050425883 and the region us-east-2 before any AWS call.
