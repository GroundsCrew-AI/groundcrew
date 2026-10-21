# Phase 6 progress: groundkrew.com

Brief: `docs/prompts/phase6.md`. Plan: `docs/phase6-plan.md`. Re-read both after a context compaction.

## Status

| Milestone | State | PR |
|---|---|---|
| P6.1 Plan | done 2026-10-21, waiting for review | draft PR `phase6-plan` |
| P6.2 Code hardening | not started | |
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

Decisions: waiting for answers to 6.1 to 6.6 (plan section 11).

Open issues:
- Let's Encrypt is ending OCSP; stapling may have nothing to staple. Checked in P6.7.
- Terraform state location (decision 6.4).

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
