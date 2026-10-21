Phase 6: run Groundcrew as a public, multi-tenant platform at groundkrew.com, entirely on ONE EC2 instance.
Treat it as a production service for external users. Read PROJECT_CONTEXT.md, docs/phase4-infra.md and
docs/phase5-interfaces.md. Save this prompt as docs/prompts/phase6.md and keep docs/phase6-progress.md updated after
every milestone (done, decisions, open issues, cost, how to resume). Re-read both after any context compaction.

Wording rule: UI, docs and comments describe Groundcrew as a product. Never write "demo", "hackathon", "for judges",
"for testing" or similar in anything user-facing.

## Constraints
- Everything runs on one EC2 instance: web, API, worker, PostgreSQL (with pgvector), nginx, certbot and builds.
  No RDS, Route 53, SES, S3, Secrets Manager, CloudWatch, SSM, load balancer or NAT.
  Allowed AWS pieces: the EC2 instance, its EBS volume, an Elastic IP, a security group, and an IAM instance role.
- AWS account 881050425883, region us-east-2 only. Verify the account before any AWS call.
- No AWS write call, apply, or change to anything outside the repo without my explicit OK in this chat. Read-only
  calls are fine. Show plans and diffs first.
- Infrastructure as code: Terraform in infra/platform/ (own state key, tags project=groundcrew,
  groundcrew:role=platform) for the instance, volume, Elastic IP, security group and instance role. Server setup as
  code too (cloud-init plus versioned scripts in infra/platform/host/), so the server can be rebuilt from scratch.
- Dev-only paths must be impossible in production (APP_ENV=production): kind-register, self-approve, dev evidence
  endpoint, test auth, deploy fixtures, local kind target. Tests proving each one refuses.
- Small commits, one draft PR per milestone, stop for review after each milestone.

## Server
- Ubuntu 24.04 LTS, x86, one encrypted gp3 root volume sized for the database, images and build cache.
  IMDSv2 required. Unattended security upgrades. Size the instance for web + API + worker + Postgres + builds;
  propose an instance type with monthly cost (I expect around t3.large with swap).
- Security group: 80 and 443 from anywhere; 22 only from my admin IP (variable). SSH key-only, no passwords, no
  root login, fail2ban. Host firewall (ufw) matching the security group. Nothing else listening publicly: Postgres,
  the API and the build daemon bind to internal Docker networks or localhost only.
- nginx as the only public entry: groundkrew.com and www (web), api.groundkrew.com (API and SSE with buffering off
  and long timeouts). certbot with automatic renewal (systemd timer) and a renewal dry-run check. HTTP redirects to
  HTTPS. HSTS, TLS 1.2+, OCSP stapling.
- DNS: I add A records at my registrar for groundkrew.com, www and api pointing to the Elastic IP. Tell me exactly
  which records.
- Docker Compose for all services with restart policies, health checks, non-root containers, read-only filesystems
  where possible, resource limits, and log rotation.
- PostgreSQL 16 + pgvector in a container with its data on the encrypted root volume (named Docker volume), password
  auth, reachable only on the internal network. No backups or snapshots.
- Secrets in root-only files (/etc/groundcrew/, 0600), loaded by Compose. Never in images, git, Terraform state or
  logs. A script to generate them and docs to rotate each one.
- Platform identity: the EC2 instance role replaces the groundcrew-platform IAM user. Its only permission is
  sts:AssumeRole on role/groundcrew-*. Update infra/aws-connect.yaml so GroundcrewPrincipalArn is this role, and
  document how existing connections move over.

## Builds of user code (untrusted)
Run builds in a separate, isolated Docker daemon on the same host: rootless, under its own system user, with gVisor
(runsc) as the container runtime for build and smoke containers, its own network with no route to Postgres, the API,
the worker or the instance metadata service (block 169.254.169.254), egress allowed for dependency downloads,
per-build CPU, memory, disk, process and time limits, no privileged mode, no host mounts, and full cleanup after each
build. The worker talks to it over a local socket or mutual TLS. Registry credentials are passed per build and never
stored. Tests and a live check that a build cannot reach Postgres, the API or the metadata service.

## Releases (no SSH from CI, no AWS keys in GitHub)
GitHub Actions builds the images, scans them with Trivy (fail on critical), and pushes them to GitHub Container
Registry with immutable tags. The server pulls releases itself: a small updater (systemd timer) checks for a new
release tag, pulls, runs migrations, restarts services one at a time, checks health, and rolls back to the previous
release automatically on failure. Manual deploy and rollback commands documented too.

## Product requirements for public users
- Sign in with GitHub (Auth.js) and with email magic links over SMTP (provider configurable; I'll use Resend or
  Gmail). Links single-use, short-lived. SPF/DKIM/DMARC records for groundkrew.com documented for me to add. Same
  verified email = same user.
- Production GitHub App separate from the dev one: installable on any account, callback and webhook URLs on
  api.groundkrew.com, webhook secret verified. Tell me exactly which settings to create.
- Workspaces: each new user gets a personal workspace. Owners invite members by GitHub username or email with roles
  owner, approver (approves fixes and deploys), viewer (read only). Strict tenant isolation: every query scoped by
  membership; a test suite trying cross-workspace access on every route must get 404.
- Approvals only by owner/approver; audit log of who approved what and when.
- Connecting AWS without S3: serve aws-connect.yaml from https://groundkrew.com, and guide users to download it and
  use "Upload a template file" in CloudFormation, with parameters shown to copy (ExternalId etc.), plus an equivalent
  aws cloudformation create-stack command. Verify as today.
- Quotas and spend protection, configurable: per-workspace daily LLM spend cap, global daily and total caps with a
  clear "limit reached" message, Tavily calls per workspace per day, concurrent runs per workspace, builds per
  workspace per day with a max build time, max connections per workspace. Usage visible in Settings.
- Abuse protection: rate limits per IP and per user on auth, API and SSE (nginx and app), request size limits,
  signup throttling, and an admin switch to pause signups.
- Security headers: CSP, HSTS, frame-ancestors, Referrer-Policy, Permissions-Policy. Cookies Secure, HttpOnly,
  SameSite. CSRF protection on state-changing routes. CORS limited to https://groundkrew.com.
- Account and data control: delete a connection and its stored data, delete a workspace, delete my account. Settings
  shows what Groundcrew can access per connection.
- Public pages: landing page at groundkrew.com (what Groundcrew does, Get started), Terms of Service, Privacy Policy
  (what is read and stored; secret values never read), Security page (permission model, read-only by default,
  approvals), docs for connecting AWS, GitHub, Vercel and Kubernetes.

## Operations
- Health endpoints. Structured logs with rotation and 30-day retention on disk.
- docs/operations.md: deploy, rollback, rotate secrets, rebuild the server from scratch, renew certificates,
  disable signups, change quotas, handle a platform incident.
- Monthly cost estimate.

## Milestones (stop after each)
P6.1 Plan: instance size and cost, Terraform and host layout, build isolation design, list of everything I must do
     by hand. No writes.
P6.2 Code hardening: production config, dev-path lockout, headers, cookies/CSRF/CORS, rate limits, quotas, roles and
     invitations, tenant isolation tests, deletion, public pages. Tests.
P6.3 Email sign-in over SMTP, production GitHub App support, AWS connect by template upload.
P6.4 Terraform for instance, volume, Elastic IP, security group and instance role. Plan, then apply after my OK.
P6.5 Host setup: cloud-init and scripts (Docker, gVisor, rootless build daemon, nginx, certbot, Postgres, firewall,
     fail2ban).
P6.6 Release pipeline (GHCR, Trivy, pull-based updater with rollback).
P6.7 First production deploy: DNS live, TLS, migrations, smoke tests over HTTPS, build isolation live check.
P6.8 End-to-end on production: GitHub and email sign-in, connect AWS by template upload, GitHub App install,
     discovery, a deploy to a connected ECS target, an incident with approval, quotas enforced, tenant isolation
     checked live.
P6.9 Runbook complete, final report.

Start with P6.1 and stop.
