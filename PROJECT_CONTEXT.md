# PROJECT_CONTEXT.md — Groundcrew

> Paste this file into any AI chat or coding agent before asking it to write code.
> Keep it in the repo root. Update the **Progress** and **Decisions log** sections at the end of every phase.
> For Claude Code you can also copy it to `CLAUDE.md`; for other agents, `AGENTS.md`.

---

## 1. What we are building

**Groundcrew** is an AI DevOps team made of NVIDIA Nemotron agents. It connects to a customer's existing
infrastructure on any cloud (AWS, Azure, Kubernetes, VMs, Vercel), maps everything into one cross-cloud
resource graph, deploys apps, and investigates and fixes incidents.

Core promises (never break these):
1. **Every fix is verified** in a sandbox or by re-checking signals before it is called done.
2. **Nothing changes production without human approval** (or an explicit customer policy).
3. **Agents read freely, write only through approved, typed actions or pull requests.**
4. **Works with the customer's existing tools** (their pipeline, their IaC). We open PRs, we do not bypass them.

Audience: small teams and agencies without a dedicated DevOps engineer, running apps across one or more clouds.

---

## 2. Hackathon constraints (Nebius x NVIDIA Global AI Hackathon)

- **Deadline:** Oct 30, 2026, 10:00 PDT. **We submit Oct 29.**
- **Track:** Coding and Agentic Engineering. **Bonus:** Best Use of Tavily (needs a real runtime Tavily call).
- **Must** make runtime calls to **Nebius Token Factory** using at least one **NVIDIA open model (Nemotron)**.
- **Must** have a **public GitHub repo** with **MIT or Apache 2.0** license visible in the About section.
- **Must** include README with setup steps, which NVIDIA models are used where, and how Token Factory and Nebius services are used.
- **Must** have a **working hosted demo** with judge login. **Keep it online until Dec 15, 2026** (judging Dec 1–15).
- **Must** have a demo video **under 3 minutes**, public on YouTube, no copyrighted music.
- **New code only.** Inspired by our earlier internal project (Code2Cloud). Do NOT copy code from it; if any is reused, it must be disclosed.
- Judging criteria (equal weight): Technological Implementation, Design (complete product), Potential Impact, Quality of the Idea.
- Stage One rejects projects that are "a superficial rebrand." The agents must do the real work.

---

## 3. Tech stack

| Layer | Choice |
|---|---|
| Frontend | Next.js 15 (App Router), TypeScript, Tailwind, shadcn/ui, React Flow (map), Auth.js (GitHub OAuth) |
| Live updates | Server-Sent Events (SSE) |
| Backend | Python 3.12, FastAPI, Pydantic v2 |
| DB | Postgres + pgvector (Neon for the hosted demo, `pgvector/pgvector:pg16` container for local dev and CI), SQLAlchemy 2 async + asyncpg, Alembic |
| Queue | Postgres `jobs` table, `SELECT ... FOR UPDATE SKIP LOCKED`, one `worker.py` process |
| LLM | OpenAI-compatible client pointed at Nebius Token Factory |
| Models | Nemotron 3 Nano / Super / Ultra (exact IDs in `.env`, see section 6) |
| Agent loop | Our own tool-calling loop (no LangChain / CrewAI) |
| Build sandbox | Nebius Serverless Jobs (fallback: Docker on our VM) |
| Web research | Tavily API |
| Secrets | Fernet encryption, key from env (`SECRETS_KEY`) |
| Cloud SDKs | boto3, azure-identity + azure-mgmt-*, azure-mgmt-resourcegraph, kubernetes, PyGithub / GitHub App JWT, paramiko (SSH) |
| Hosting | Web on Vercel; API + worker + k3s on one small VM with Docker Compose + Caddy |
| CI | GitHub Actions: ruff, mypy, pytest, eslint, tsc |

---

## 4. Repository layout

```
groundcrew/
├── PROJECT_CONTEXT.md
├── LICENSE
├── README.md
├── docker-compose.yml
├── .github/workflows/ci.yml
├── apps/
│   ├── web/                      # Next.js frontend
│   └── api/
│       ├── app/
│       │   ├── main.py
│       │   ├── config.py
│       │   ├── db/               # models, session, alembic migrations
│       │   ├── routes/           # REST + SSE
│       │   ├── llm/              # client.py, router.py, costs.py
│       │   ├── agents/           # base.py, scout.py, builder.py, deployer.py, medic.py
│       │   ├── tools/            # registry.py + one module per tool group
│       │   ├── policy/           # allowlists, approval rules
│       │   ├── connectors/       # aws.py, azure.py, github.py, kubernetes.py, vm_ssh.py, vercel.py
│       │   ├── targets/          # base.py, k8s.py, aws_ecs.py, nebius.py, vm_ssh.py
│       │   ├── graph/            # normalize.py, edges.py (relationship inference), coverage.py
│       │   ├── telemetry/        # cloudwatch.py, azure_monitor.py, prometheus.py, loki.py, events.py
│       │   └── worker.py
│       └── tests/
├── infra/
│   ├── aws-connect.yaml          # CloudFormation cross-account role for customers
│   ├── azure-connect.bicep       # Azure federated identity setup
│   └── hosting/                  # our deploy (compose, Caddyfile)
└── demo/
    ├── sample-app/               # app we deploy and break
    ├── aws/                      # Terraform (ECS Fargate worker, ECR, RDS)
    └── azure/                    # Terraform (App Service or Container App api)
```

---

## 5. Architecture (short)

```
Web app (Vercel) ──REST+SSE──▶ Control plane (FastAPI on VM)
                                 ├─ API server
                                 ├─ Agent orchestrator  (tool loop + policy + approvals)
                                 ├─ Model router        ──▶ Nebius Token Factory (Nemotron)
                                 ├─ Worker (job queue)  ──▶ Nebius Serverless Jobs (builds)
                                 ├─ Tavily client       ──▶ Tavily
                                 └─ Postgres (Neon + pgvector)
                                        │
                     reads freely; writes need approval
                                        ▼
        Customer infra: AWS (STS role) · Azure (federated ID) · GitHub (App) · K8s (scoped SA) · VM (SSH)
```

---

## 6. Agents and model routing

| Agent | Job | Model | Allowed tools |
|---|---|---|---|
| Scout | Read repos + graph; output `app_spec`; summarize systems | Nemotron 3 **Nano** | read repo, read graph |
| Builder | Write Dockerfile, build in sandbox, patch + retry (max 5) | Nemotron 3 **Super** | write file (sandbox only), run build |
| Deployer | `app_spec` → target config; preflight; deploy; verify | Nemotron 3 **Super** | target plan / apply (approval) / verify |
| Medic | Cross-cloud root cause; propose + verify fixes | Nemotron 3 **Ultra** | read telemetry, Tavily, open PR, emergency actions (approval) |

Model routing (`app/llm/router.py`): classify / summarize / detect → Nano; write_config / plan / patch → Super;
root_cause → Ultra. `DEV_MODE_NO_ULTRA=true` sends Ultra tasks to Super. Only tiers in `LLM_REASONING_TIERS`
(default Ultra) reason before answering; Nano and Super calls send `enable_thinking=false`.

Env vars (exact model IDs must be copied from the Token Factory docs; `.env.example` has a comment for each).
Blank means unset; defaults in brackets.
```
# Token Factory and models
TOKEN_FACTORY_BASE_URL=
TOKEN_FACTORY_API_KEY=
MODEL_NANO=
MODEL_SUPER=
MODEL_ULTRA=
MODEL_EMBED=                  # NVIDIA embedding model if available
EMBED_DIM=                    # [1024] incidents.embedding size; changing it needs a migration
LLM_TIMEOUT_SECONDS=          # [60]
LLM_MAX_TOKENS=               # [4096] per call, reasoning included
LLM_MIN_COMPLETION_TOKENS=    # [512] stop the run instead of a call with less room than this
LLM_REASONING_TIERS=          # [ultra] tiers that think first; others send enable_thinking=false
DEV_MODE_NO_ULTRA=            # [false] route Ultra tasks to Super
PRICE_NANO_INPUT_PER_M=       # optional USD per 1M tokens; unset = cost not estimated
PRICE_NANO_OUTPUT_PER_M=
PRICE_SUPER_INPUT_PER_M=
PRICE_SUPER_OUTPUT_PER_M=
PRICE_ULTRA_INPUT_PER_M=
PRICE_ULTRA_OUTPUT_PER_M=

# Services and secrets
TAVILY_API_KEY=
DATABASE_URL=                 # Neon, hosted demo only. Local dev / tests use the compose container
SECRETS_KEY=                  # Fernet key: make keygen

# GitHub App (Phase 3)
GITHUB_APP_ID=
GITHUB_APP_CLIENT_ID=
GITHUB_APP_CLIENT_SECRET=
GITHUB_APP_PRIVATE_KEY_PATH=  # PEM file in secrets/ (git-ignored)
GITHUB_WEBHOOK_SECRET=

# Budgets and limits
DAILY_TOKEN_BUDGET=           # [no limit] tokens per UTC day, all agents
MAX_ULTRA_CALLS_PER_INCIDENT= # [no limit]
MAX_STEPS_PER_RUN=            # [12] model calls per run
MAX_TOKENS_PER_RUN=           # [60000]

# Worker, SSE, logging
WORKER_POLL_INTERVAL_SECONDS= # [1.0]
JOB_LOCK_TIMEOUT_SECONDS=     # [600] a job whose worker stopped heartbeating is reclaimed after this
WORKER_SHUTDOWN_GRACE_SECONDS= # [20] on SIGTERM, then the job is requeued
SSE_POLL_INTERVAL_MS=         # [500]
LOG_LEVEL=                    # [INFO]

# Auth (Phase 2). Secrets stay server side; only NEXT_PUBLIC_API_URL reaches the browser.
INTERNAL_JWT_SECRET=          # shared by web and API: HS256 key for the 5-minute internal JWT (32+ chars)
WEB_ORIGIN=                   # [http://localhost:3000] the only origin allowed by CORS (for SSE)
AUTH_SECRET=                  # Auth.js session encryption key
AUTH_GITHUB_ID=               # [GITHUB_APP_CLIENT_ID] GitHub OAuth client for sign-in
AUTH_GITHUB_SECRET=           # [GITHUB_APP_CLIENT_SECRET]
API_INTERNAL_URL=             # [NEXT_PUBLIC_API_URL] where the web server reaches the API
NEXT_PUBLIC_API_URL=          # [http://localhost:8000] where the browser opens the run stream
```

---

## 7. Security and policy rules (agents writing code MUST follow these)

1. **No long-lived cloud credentials.** AWS = `sts:AssumeRole` with `ExternalId` per workspace. Azure = federated credential. Never store AWS keys.
2. **Encrypt every stored secret** (kubeconfigs, SSH keys, tokens) with Fernet. Never log secrets. Redact tokens in errors.
3. **Every tool declares a policy tag:** `read`, `write`, or `emergency`.
   - `read` tools run immediately.
   - `write` and `emergency` tools **never run directly**: they create an `approvals` row and wait.
4. **Generic cloud read tools** (AWS Cloud Control `get`/`list`, Azure Resource Manager GET) are allowed **only** for read verbs: `Get*`, `List*`, `Describe*`. Enforce with an allowlist + unit tests.
5. **Never read customer data** (database rows, object contents). Metadata, config, logs, metrics only.
6. **Fix path preference:** pull request to IaC/code → re-run pipeline → typed emergency action (rollback/restart/scale) only with approval, followed by a PR.
7. **Budgets:** max steps per run, max tokens per run, `DAILY_TOKEN_BUDGET`, `MAX_ULTRA_CALLS_PER_INCIDENT`. Stop the run cleanly with a `stop_reason` when hit.
8. **Every action is logged** in `run_steps` / `approvals` with who approved it.

---

## 8. Core interfaces

```python
class Target(Protocol):
    def discover(self) -> list[Resource]: ...
    def plan(self, spec: AppSpec) -> Plan: ...
    def preflight(self, plan: Plan) -> list[Check]: ...
    def apply(self, plan: Plan) -> Deployment: ...          # behind approval
    def verify(self, dep: Deployment) -> Health: ...
    def logs(self, dep: Deployment, since: datetime) -> list[LogLine]: ...
    def rollback(self, dep: Deployment) -> Deployment: ...  # behind approval
```

Normalized resource (every cloud maps into this):
```
Resource: id, workspace_id, provider (aws|azure|gcp|k8s|vercel|vm), type, region, name,
          tags, config_json, coverage (observed|diagnosable|actionable), iac_path, repo
Edge:     id, from_id, to_id, kind (calls|reads|deployed_by|defined_in), evidence
```

---

## 9. Data model (tables)

`users` (upserted by `github_id`), `workspaces`, `workspace_members` (workspace_id, user_id, role: owner | member),
`connections`, `resources`, `edges`, `runs`, `run_steps` (with `approval_id` on approval steps), `approvals`
(`decided_by` = user id), `incidents` (with `embedding`), `events` (normalized telemetry timeline), `jobs`.

## 10. Main API

| Method | Path | Purpose |
|---|---|---|
| POST | `/connections` | Add AWS / Azure / GitHub / cluster / VM connection |
| POST | `/workspaces/{id}/discover` | Start discovery |
| GET | `/workspaces/{id}/graph` | Resources + edges |
| POST | `/deploys` | Scout → Builder → Deployer |
| POST | `/incidents` | Open incident, start Medic |
| GET | `/me` | Signed-in user and their workspaces |
| GET | `/workspaces/{id}/runs` | Runs, newest first (`limit`, `cursor`) |
| GET | `/workspaces/{id}/approvals` | Approvals (`status=pending`, or `decided=true` for history) |
| GET | `/workspaces/{id}/summary` | Overview counts: runs today, pending approvals, tokens vs budget |
| POST | `/runs` | Start a run |
| GET | `/runs/{id}` | Run with steps and approvals |
| POST | `/runs/{id}/stop` | Stop a queued, running, or waiting run (`stopped_by_user`) |
| POST | `/runs/{id}/stream-token` | 60-second token for the SSE stream, bound to run and user |
| GET | `/runs/{id}/events?token=` | SSE: `step`, `approval_requested`, `approval_decided`, `status`, `done` |
| POST | `/approvals/{id}/decide` | Approve / reject as the signed-in user |
| POST | `/demo/inject/{scenario}` | Break or restore demo app |

---

## 11. Coding conventions

- Python: type hints everywhere, Pydantic models for all I/O, `ruff` + `mypy --strict` clean, pytest for every tool and policy rule.
- TypeScript: strict mode, API client generated from FastAPI OpenAPI, shadcn/ui components, no `any`.
- Small PRs, one task each; CI must pass before merge.
- No new dependency without a one-line reason in the PR description.
- No mocks in the final demo path; mocks only in tests.
- Copy and UI text: short, specific, plain English. No em dashes, no hype words.

## 12. Cost rules

- Never create AWS NAT Gateways or Application Load Balancers for the demo.
- One AWS region, one Azure region. Tag all demo resources `project=groundcrew`.
- Budget alerts: AWS $20 / $50, Azure $20.
- Demo infra lives in Terraform; destroy after Dec 15.

---

## 13. Phase plan (summary)

| Phase | Dates | Goal |
|---|---|---|
| 0 Setup | Oct 5–6 | Credits, repo, license, accounts, budget alerts, test call to each Nemotron model |
| 1 Backend + agent loop | Oct 7–10 | FastAPI, DB, queue, Token Factory client, router, tool loop, SSE |
| 2 Frontend shell | Oct 8–12 | Login, layout, live run timeline, approvals inbox |
| 3 Discovery + graph | Oct 11–16 | Demo env, AWS/Azure/GitHub/K8s connections, inventory, edges, map |
| 4 Build + deploy agents | Oct 15–20 | Scout, Builder (retry loop), Deployer; K8s + ECS + Nebius targets |
| 5 Medic + fixes | Oct 19–24 | Telemetry, incidents, Tavily, PR fixes, emergency actions, memory |
| 6 Hardening + hosting | Oct 24–27 | Hosted demo, judge account, inject-failure button, security pass |
| 7 Submission | Oct 26–29 | README, video, Devpost text, feedback, submit Oct 29 |
| 8 Judging upkeep | Oct 30–Dec 15 | Keep demo online, check twice weekly, destroy after Dec 15 |

Feature freeze: **Oct 25.** Cut order if late: Navigator/migration → Docker Compose target → incident memory → VM target → Prometheus/Loki.

---

## 14. Progress (update at the end of every phase)

- [ ] Phase 0 — Setup
  - [x] Monorepo scaffold (section 4 layout, READMEs in empty folders)
  - [x] API skeleton: FastAPI `/health`, settings, tests, Dockerfile (uv)
  - [x] Web skeleton: Next.js 15, Tailwind, shadcn/ui, health status page
  - [x] Token Factory smoke test script (`make models`)
  - [x] Local dev: `.env.example`, `.gitignore`, docker-compose, Makefile
  - [x] CI: ruff, mypy, pytest, eslint, tsc, next build
  - [x] Starter README
  - [x] Code pushed to GitHub, CI passing
  - [x] Token Factory credits; test call to Nemotron 3 Nano, Super, and Ultra (`make models`, 3/3 ok)
  - [x] AWS and Azure accounts
  - [x] Neon Postgres project, `DATABASE_URL` in `.env` (pooled, `sslmode=require`; Postgres 18.6, pgvector 0.8.6 available, connection verified)
  - [ ] Tavily account and API key
  - [ ] Budget alerts: AWS $20 / $50, Azure $20 (section 12)
- [x] Phase 1 — Backend + agent loop
  - [x] Postgres (pgvector container), SQLAlchemy async models for all section 9 tables, Alembic, `make seed`
  - [x] Fernet secrets helper, `make keygen`
  - [x] Postgres job queue (`SKIP LOCKED`, 3 attempts with backoff) and worker with graceful shutdown
  - [x] LLM layer: Token Factory client, router, costs, budgets, native + text tool calls
  - [x] Tool registry with policy tags; policy engine; approvals pause and resume runs
  - [x] Agent loop with limits, stop reasons, redacted step recording; Echo Ops toy agent
  - [x] API: runs, run SSE events, approvals; JSON logs with request id
  - [x] Tests on real Postgres with a fake LLM; CI runs migrations and tests without secrets
  - [x] Verified end to end with Nemotron Nano: run pauses at `restart_fake_service`, approve, run completes
  - [x] Review fixes: job heartbeat and owner checks, dead jobs fail their run, shutdown requeues the job,
        SSE no longer holds a DB connection, no transaction open during model or tool calls, invalid tool
        arguments kept and reported, cut-off answers fail the run, per-call token caps, unique tool call IDs,
        log extras and approval arguments redacted (101 tests)
  - [x] Reasoning off for Nano and Super (`LLM_REASONING_TIERS`, default ultra). Echo Ops end to end on Nano:
        3 calls, 64 completion tokens in total (was 700 to 950 per call with reasoning on)
- [x] Phase 2 — Frontend shell
  - [x] API auth: internal JWT from the web proxy, users upserted by GitHub id, first-login workspace,
        `workspace_members` with 404 for non-members, stream tokens, stop endpoint, typed SSE events
  - [x] Approvals record the real user; responses carry the decider's login, name, and avatar
  - [x] Web: Auth.js GitHub sign-in, middleware, `/api/gc` proxy, typed client (`make api-types`)
  - [x] App shell: sidebar with workspace switcher and pending badge, user menu, mobile drawer, themes
  - [x] Overview, Runs list, New run dialog, live run timeline with inline approvals and Stop,
        Approvals inbox (pending and decided), placeholders for Map, Deploy, Incidents, Connections, Settings
  - [x] Tests: 127 API (auth, membership, stream tokens, stop), 17 web (reducer, approval card, JWT);
        CI runs vitest and checks generated API types; no secrets needed
  - [x] Verified in Chrome with an Auth.js session for a test GitHub identity: start a run from the UI,
        live steps, refresh mid-run (no duplicates), approve inline, decider shown, second user gets 404,
        signed-out URLs redirect to sign-in
  - [x] Rejections read honestly: the model is told the exact rejected action and to say it was rejected;
        the step is recorded as `tool_rejected` and shown as "Not run: rejected"; runs carry
        `rejected_actions`, shown as "N actions rejected" beside the status (verified live on Nano)
  - [x] Real GitHub sign-in works (approvals recorded as the signed-in GitHub user)
- [ ] Phase 3 — Discovery + graph
- [ ] Phase 4 — Build + deploy agents
- [ ] Phase 5 — Medic + fixes
- [ ] Phase 6 — Hardening + hosting
- [ ] Phase 7 — Submission
- [ ] Phase 8 — Judging upkeep

**Current phase:** 3
**Last updated:** 2026-10-06
**Known issues / blockers:**
- Phase 0 open items: Tavily API key; budget alerts (AWS $20 / $50, Azure $20).
- Any member of a workspace can approve. Roles (who may approve write vs emergency actions) come with
  Settings in Phase 6.
- The browser opens the run stream directly on the API, so the hosted API needs `WEB_ORIGIN` set to the
  web app's URL and must be reachable from the browser (Phase 6).
- The daily token budget is checked without a lock. Exact with one worker; with several workers the
  overshoot is at most one capped call per worker.
- Tool calling with reasoning off is verified only on the Echo Ops toy agent. Re-check quality when
  Scout, Builder, and Deployer land, and add their tiers to `LLM_REASONING_TIERS` if needed.
- The demo runs locally only; nothing in Phase 1 targets Neon. Run `make migrate DB_URL=<neon url>` when hosting.

## 15. Decisions log (newest first)

| Date | Decision | Why |
|---|---|---|
| 2026-10-06 | A run whose action was rejected still ends `succeeded` / `completed`, with a `rejected_actions` count shown beside the status | The agent did finish cleanly; the marker keeps it from reading as a full success, without a new stop reason or migration |
| 2026-10-06 | Sign-in with Auth.js (NextAuth v5) and the GitHub provider, using our GitHub App's OAuth client | One identity for login now and repo access in Phase 3; no passwords to store |
| 2026-10-06 | REST goes browser -> Next.js `/api/gc/*` proxy -> FastAPI with a 5-minute HS256 internal JWT (`INTERNAL_JWT_SECRET`) | The browser never holds an API credential, and REST needs no CORS |
| 2026-10-06 | SSE goes browser -> FastAPI directly with a 60-second stream token bound to run and user; CORS only for `WEB_ORIGIN` | Long-lived streams time out behind serverless proxies |
| 2026-10-06 | TanStack Query plus a client generated from FastAPI's OpenAPI schema (openapi-typescript, openapi-fetch); CI fails on drift | One typed contract between API and web, no hand-written types |
| 2026-10-06 | Users upserted by GitHub numeric id; a workspace per user on first login; non-members get 404, not 403 | Login renames do not split accounts; workspace ids cannot be probed |
| 2026-10-06 | Stopping a run rejects its pending approvals, recorded as decided by the user who stopped it | The inbox never shows actions for runs that cannot resume |
| 2026-10-06 | Reasoning off by default for Nano and Super (`enable_thinking=false`), on for Ultra (`LLM_REASONING_TIERS`) | Verified on all three models: a test reply went from 20+ tokens with no answer to 2 tokens; Echo Ops tool calls went from 700 to 950 completion tokens per call to under 30. Medic's root cause work keeps reasoning |
| 2026-10-06 | Worker heartbeats its job lock; only the lock owner can finish a job; budgets cap each call's max_tokens | A long run is never run twice, a dead job never leaves a run stuck, and one call cannot blow far past a budget |
| 2026-10-06 | Local dev and CI use a `pgvector/pgvector:pg16` container (host port 5433); Neon only for the hosted demo | Fast, free, resettable; CI needs no secrets; avoids clashing with a host Postgres on 5432 |
| 2026-10-06 | SQLAlchemy 2.0 async with asyncpg; Alembic migrations in `apps/api/app/db/migrations` | Async fits FastAPI and the worker; Alembic gives reviewable schema changes |
| 2026-10-06 | SSE streams run steps by polling the DB every ~500 ms, not LISTEN/NOTIFY | Neon's pooler does not support LISTEN/NOTIFY; polling is simple and good enough |
| 2026-10-06 | Own agent loop, no framework; conversation stored in `runs.messages` so a run pauses for approval and resumes in a new job | Full control of the policy gate and step log; resume is the same code path as a fresh run |
| 2026-10-06 | An approved write is marked used and committed before it runs | A write action runs at most once, even if the worker crashes mid-call |
| 2026-10-06 | `GITHUB_APP_PRIVATE_KEY` replaced by `GITHUB_APP_PRIVATE_KEY_PATH` (file in git-ignored `secrets/`) | Multi-line PEM keys are fragile in .env files |
| 2026-10-06 | Models: Nano `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`, Super `nvidia/nemotron-3-super-120b-a12b`, Ultra `nvidia/Nemotron-3-Ultra-550b-a55b` | Exact IDs from the Token Factory model list, all verified with `make models`; avoids models removed on 2026-08-31 |
| 2026-10-06 | uv for Python dependencies and lockfile (`apps/api/uv.lock`) | Fast, reproducible installs; one tool for venv, lock, and run |
| 2026-10-05 | Own agent loop, no framework | Full control over guardrails and timeline |
| 2026-10-05 | Postgres as queue + vector store | No extra services to run or pay for |
| 2026-10-05 | Fixes go through PRs; emergency actions need approval | Works with customer pipelines; safe by default |
| 2026-10-05 | Track: Coding and Agentic Engineering + Tavily bonus | Best fit for agents that write, run, and test infra code |

---

## 16. How to ask an AI agent for help

Start every request like this:

```
Context: see PROJECT_CONTEXT.md (pasted above).
Current phase: <N>. Task: <one task from the roadmap checklist>.
Constraints: follow sections 7 (security) and 11 (conventions). Don't change files outside <folder>.
Done when: <the check from the roadmap>.
```
