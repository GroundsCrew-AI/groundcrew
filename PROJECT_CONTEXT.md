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

Model routing (`app/llm/router.py`) has two dials per task: a tier and a reasoning mode
(`off` = `enable_thinking: false`, `low` = `enable_thinking: true, low_effort: true`, `on` = `enable_thinking: true`).

| Task | Tier | Reasoning | Max tokens | Fallback |
|---|---|---|---|---|
| summarize_graph, compress_evidence, classify_signal | Nano | off | 300 to 1,500 | |
| verify_recovery | Nano, or **Lightning** once priced | off | 800 | Nano when on Lightning |
| detect_app_spec (Scout) | Nano | low | 2,000 | Super |
| write_dockerfile, plan_deploy | Super | low | 3,000 | |
| fix_build | Super | on | 6,000 | |
| tool_step, investigate_step | Super, or **Lightning** once priced | off | 1,500 | Nano (tool_step); Super when on Lightning |
| root_cause | Ultra | on | 10,000 | Super |
| propose_fix | Ultra | on | 6,000 | Super |
| classify, summarize, detect / write_config, plan, patch (Phase 1 generic, Echo Ops) | Nano / Super | off | `LLM_MAX_TOKENS` | |

`escalate=True` bumps one tier, turns reasoning fully on, and gives 1.5x the tokens (Builder's 4th build attempt
goes to Ultra). Ultra and Lightning never run with reasoning `low` (in the probes it saved little or cost more).
Nemotron 3.5 Lightning is its own tier (`lightning`, escalates to Super); its routes (`LIGHTNING_ROUTES`) switch
on when `PRICE_LIGHTNING_*` is set, with no code change. Each tier has a *usable* context (`LLM_CONTEXT_*`): a
bigger prompt goes to the route's fallback tier (Nano: 8,000 tokens, so large Scout inputs go to Super).
Schema fields the code compares on are lowercase Literals with an `other` value (the client refuses a schema
without it); prompts say to use `other` or null when nothing fits; model output is normalized onto the allowed
values before validation. Evidence for Medic is ranked by embedding cosine similarity (no rerank model on
Token Factory). `DEV_MODE_NO_ULTRA=true` keeps everything on Super or below. All calls send `temperature=1.0`,
`top_p=0.95` (NVIDIA's Nemotron 3 guidance). Every call goes through `app/llm/client.py` (`TokenFactory.chat`):
retries, fallback, JSON-schema output with one repair, reasoning capture (shown, never re-sent), cost and
Ultra-equivalent cost on every step, and the spend caps in `app/llm/budget.py`.

Env vars (exact model IDs must be copied from the Token Factory docs; `.env.example` has a comment for each).
Blank means unset; defaults in brackets.
```
# Token Factory and models
TOKEN_FACTORY_BASE_URL=
TOKEN_FACTORY_API_KEY=
MODEL_NANO=
MODEL_SUPER=
MODEL_ULTRA=
MODEL_LIGHTNING=              # [nvidia/Nemotron-3_5-Lightning] routes switch on once PRICE_LIGHTNING_* is set
MODEL_EMBED=                  # [Qwen/Qwen3-Embedding-8B] returns 4096 dims; cut to EMBED_DIM and re-normalised
EMBED_DIM=                    # [1024] incidents.embedding size; changing it needs a migration
MODEL_RERANK=                 # e.g. Qwen/Qwen3-Reranker-8B; unset = rerank keeps input order
GIT_CLONE_TOKEN=              # optional, for private HTTPS clones; sent via git's environment
GIT_CLONE_TIMEOUT_SECONDS=    # [120]
REPO_MAX_BYTES=               # [300000000] bigger clones are refused and deleted
REPO_WORKDIR=                 # [system temp] parent dir for temporary clones
LLM_CONTEXT_NANO=             # [8000] usable context: accepts 65k, answers wrong from ~20k
LLM_CONTEXT_SUPER=            # [256000]
LLM_CONTEXT_ULTRA=            # [256000]
LLM_CONTEXT_LIGHTNING=        # [64000] read 60k-token prompts correctly
LLM_TIMEOUT_SECONDS=          # [120]
LLM_MAX_ATTEMPTS=             # [3] per model on 429/5xx/connection errors; a timeout is retried once at most
LLM_RETRY_BASE_SECONDS=       # [1.0] backoff base * 2**n plus jitter
LLM_MAX_TOKENS=               # [4096] per call for generic agent-loop tasks; pipeline tasks set their own
LLM_MIN_COMPLETION_TOKENS=    # [512] stop the run instead of a call with less room than this
DEV_MODE_NO_ULTRA=            # [false] route Ultra tasks to Super
PRICE_NANO_INPUT_PER_M=       # [0.06] USD per 1M tokens. Defaults UNVERIFIED (third-party listing):
PRICE_NANO_OUTPUT_PER_M=      # [0.24]   set the real ones from the Token Factory pricing page.
PRICE_SUPER_INPUT_PER_M=      # [0.30]   A model with no price is never called.
PRICE_SUPER_OUTPUT_PER_M=     # [0.90]
PRICE_ULTRA_INPUT_PER_M=      # [1.00]
PRICE_ULTRA_OUTPUT_PER_M=     # [3.00]
PRICE_LIGHTNING_INPUT_PER_M=  # no default: Lightning is refused, and its routes stay off, until both are set
PRICE_LIGHTNING_OUTPUT_PER_M=
PRICE_EMBED_PER_M=            # no default: embeddings are refused until set
PRICE_RERANK_PER_M=           # no default: rerank is refused until set (if MODEL_RERANK is set)

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

# Budgets and limits (USD caps count in the llm_usage table; the total cap is the hard stop)
LLM_RUN_USD_CAP=              # [0.40] per run
LLM_DAILY_USD_CAP=            # [2.00] per UTC day, all runs and scripts; 0.50 on the hosted demo
LLM_TOTAL_USD_CAP=            # [25] all time; credits are about $30
MAX_ULTRA_CALLS_PER_RUN=      # [3]
DAILY_TOKEN_BUDGET=           # [no limit] tokens per UTC day, all agents
MAX_ULTRA_CALLS_PER_INCIDENT= # [no limit]
MAX_STEPS_PER_RUN=            # [12] model calls per run
MAX_TOKENS_PER_RUN=           # [400000]

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
- [x] Phase 3 — Discovery + graph (complete 2026-10-09: PRs #6, #7, #8; verified live on the rebuilt demo stack)
  - [x] AWS: `infra/aws-connect.yaml` (role trusted by `GroundcrewPrincipalArn` with `sts:ExternalId`, 1 h,
        read-only policy, explicit deny on secret values / SSM / KMS decrypt / S3 objects, Resource Explorer
        LOCAL index + default view, no AWS Config); start / complete endpoints; verify refuses a role that
        works without the ExternalId; sessions in memory only; runtime allowlist guard on every boto3 call
  - [x] GitHub App: installation tokens, GET-only client, repo picker, `GitHubAppSource` (authenticated clone)
  - [x] Vercel: token encrypted, names and types only, values only for plain *_URL / *_HOST
  - [x] Azure (Resource Graph, `infra/azure-connect.bicep`) on fixtures, marked Preview: `TODO(azure-live)`
  - [x] Kubernetes (`infra/k8s-readonly-rbac.yaml`, or the kind deployer): live since 2026-10-09 (`LiveKubeReader`,
        kubeconfig verified by listing namespaces before it is stored, `make kind-register`; see Phase 4 infra)
  - [x] Discovery engine (`app/discovery/`): jobs `discover` / `discover_workspace` as normal runs with SSE;
        AWS Resource Explorer search with tagging API fallback plus core describes; IaC facts (python-hcl2,
        workflows, Dockerfiles, compose); upsert on stable external_id; stale marking; limits and timeouts.
        Live read-only check on 881050425883: 17 resources in 8.7 s
  - [x] Edges (`app/graph/edges.py`: core kinds calls / reads / deployed_by / defined_in plus uses,
        pulls_from, logs_to; confidence and evidence), coverage, a deterministic graph digest under ~6k
        tokens for Scout's unchanged `summarize_system` (rule risks in resource notes)
  - [x] Contracts: `CredentialsProvider` (`app/connectors/credentials.py`), `GitHubAppSource`
        (`app/connectors/github_source.py`), `ResourceSink` (`app/graph/sink.py`), `DiscoveredEdge`
  - [x] API: connections, graph, resources; web types regenerated
  - [x] Web: Connections (AWS flow, GitHub picker, Vercel token, Azure / K8s Preview) and Map (React Flow +
        ELK grouped layout, evidence panel, filters, summary)
  - [x] Fixes from the first live run (#6): git in the API image (the clone failed without it); workflows read
        through the GitHub contents API when the clone fails, so the 3 `deployed_by` edges to deploy.yml survive;
        web -> checkout-api `calls` edge from the `API_URL` name, shared repo, and deploy.yml's web job (medium
        while the value is encrypted); Map fits on load, larger nodes, Vercel / AWS / GitHub side by side, ELK-placed
        labels, tooling in a collapsed Tooling group
  - [x] Summary safety without editing `agents/`: tooling (`app/graph/roles.py`) tagged `role=tooling` in the
        digest; each service's own image stated; `app/graph/summary_check.py` drops bullets the graph contradicts
        (unlinked tooling, missing resource kinds, shared images or image repositories, relationships with no
        edge, workflows "provisioning" infrastructure with no IaC apply), each recorded as a `check_summary` run
        step; Unicode hyphens normalised (Nano writes `checkout‑api`); prompt tweak proposed for Ahmed in
        `docs/phase3-interfaces.md` (#6, #7, #8)
  - [x] `scripts/replay_discovery.py`: the pipeline on the recorded demo fixtures (edge counts, Scout's summary,
        dropped bullets), for checks while the demo stack is down
  - [x] Live verification (2026-10-09, demo stack destroyed and rebuilt): old VPC / security groups / secret
        marked stale and hidden, new ones discovered; 23 resources, 13 relationships (calls 1 medium,
        deployed_by 4, logs_to / pulls_from / reads / uses 2 each) matching the fixtures; discovery in 15–17 s;
        Map and summary checked in the UI
  - [x] Tests: 437 API (allowlist, secret values never requested, ExternalId, platform identity, regions,
        upsert / stale, every edge rule, contracts, coverage, digest tooling tags, summary check with the live
        false claims, API), 40 web
  - Env vars: `GROUNDCREW_AWS_ACCESS_KEY_ID`, `GROUNDCREW_AWS_SECRET_ACCESS_KEY`,
    `GROUNDCREW_PLATFORM_PRINCIPAL_ARN`, `AWS_CONNECT_TEMPLATE_URL`,
    `DISCOVERY_REGIONS` [us-east-2], `MAX_RESOURCES_PER_CONNECTION` [2000], `DISCOVERY_TIMEOUT_SECONDS` [100]
  - Endpoints: `POST /connections/aws/start`, `POST /connections/aws/complete`, `GET /connections/aws/template`,
    `GET /connections/github/installations`, `POST /connections/github`, `POST /connections/vercel`,
    `POST /connections/azure`, `POST /connections/k8s`, `GET /connections`, `DELETE /connections/{id}`,
    `POST /connections/{id}/discover`, `POST /workspaces/{id}/discover`, `GET /workspaces/{id}/graph`,
    `GET /resources/{id}`. Contract for Phase 4/5: `docs/phase3-interfaces.md`
  - Decisions (2026-10-08): demo AWS workloads only in us-east-2 (org policy), so regional calls use
    `DISCOVERY_REGIONS` only and IAM / STS are global; read-only enforced twice (IAM policy with explicit deny,
    and a botocore hook that refuses any operation outside the allowlist); the AWS connect link needs the
    template in S3 (CloudFormation only accepts S3 URLs): private bucket `groundcrew-templates-881050425883`
    (us-east-2, versioned), `make publish-connect-template`; a private bucket only serves stacks in the same
    account. Groundcrew's AWS identity is the IAM user `groundcrew-platform` (only `sts:AssumeRole` on
    `role/groundcrew-*`), from two env vars, never profiles or `~/.aws` (docs/self-hosting.md); an
    encrypted Vercel `API_URL` gives a medium `calls` edge (name + shared repo + workflow evidence), a plain
    one compared with the task IP gives high; edges are recomputed per workspace on every discovery;
    secret-to-database links come from Terraform (secret version built from the db) before names
- [ ] Phase 4 — Build + deploy agents (in progress)
  - [x] Step 1: LLM layer. Task routing (tier + reasoning mode, fallback, escalation), `TokenFactory.chat` with
        retries, JSON-schema output and one repair, reasoning capture, `embed` / `rerank`; USD caps per run, per day,
        and in total backed by `llm_usage`; `cost_usd` and `ultra_equiv_usd` on every step and run; FakeLLM runs
        the real client code with scripted replies; `scripts/probe_token_factory.py` (run 2026-10-07)
  - [x] Step 1 follow-up (probe results): Ultra never uses reasoning `low` (`propose_fix` is Ultra/on); Literal and
        StrEnum schema values normalized before validation; embeddings default to Qwen3-Embedding-8B, ask for 1024
        dims and otherwise truncate and re-normalise; evidence ranked by embedding cosine (rerank only if
        configured); Nemotron 3.5 Lightning registered as a tier, not routed; `scripts/probe_followup.py`
  - [x] Follow-up config: usable context Nano 8,000 / Lightning 64,000 (oversize prompts go to the fallback tier),
        Lightning routes for tool_step / investigate_step / verify_recovery that switch on with its price, no
        `low` on Ultra or Lightning, every enum must allow `other`; `FEEDBACK.md` started
  - [x] Step 2: Repo source. `RepoSource` protocol with `LocalPathSource` and `GitCloneSource` (https only,
        shallow, token via git's environment, size cap, always cleaned up); read-tier `list_files` and
        `read_file` tools: sorted output, caps on entries, lines, and bytes, truncation markers, .gitignore
        respected, secret files and paths outside the repo refused, excerpts redacted
  - [x] Step 3: Scout. Rule detectors (package.json / lockfiles, requirements / pyproject / Pipfile, go.mod,
        Dockerfile, Procfile, env templates, ports, env vars, health routes in code) with file:line evidence, then
        one Nano call (detect_app_spec: low reasoning, prompt capped at ~6k tokens, Super fallback, strict schema).
        Rules win conflicts (recorded); model values with no supporting line are not applied (`low_confidence` +
        `unverified`); per-field source rule/model; deterministic confidence. `summarize_system` (Nano, off).
        Fixtures in `demo/fixtures/` with golden specs; `scripts/live_scout.py` (not run)
  - [ ] Infra (`phase4-infra`, PR #9): deploy contract (`docs/phase4-infra.md`, `GC_*` settings,
        `app/connectors/deploy_infra.py`); local kind cluster with registry, ingress, and the `groundcrew-deployer`
        ServiceAccount limited to `gc-*` namespaces (RBAC + ValidatingAdmissionPolicy, `docs/kind.md`); Kubernetes
        connections live (`TODO(k8s-live)` done); `make kind-register` (dev only, `APP_ENV=dev`); AWS deploy tier
        (`Tier=observe|deploy` in `infra/aws-connect.yaml`, `groundcrew-deploy` role scoped to `groundcrew-*` and
        `project=groundcrew`, `aws_deploy_session()`, Observe / Deploy on the Connections page; not published).
        Next: cleanup, CI, live runbook
  - Ownership (2026-10-11, agreed with Ahmed): Rizwan owns all of Phase 4 (sandbox, targets, Builder,
    Deployer, pipeline, `/deploys`, Deploy page, Phase 4 E2E). Ahmed owns Phase 5 and builds it in
    parallel (`app/agents/medic/`, telemetry, incidents), which Phase 4 never edits. Done when
    `GroundsCrew-AI/legacy-flask-app` goes from GitHub to a live URL on kind and AWS ECS in one live run
    timeline, with at least one automatic build fix and an approval before any infrastructure change.
    Milestones, one draft PR each: M1 contract, M2 sandbox, M3 targets, M4 Builder, M5 Deployer,
    M6 pipeline + API, M7 Deploy page, M8 E2E + live evidence. Order: M1, M2, M7 (against the M1
    types, with one fake response file that M6 deletes), M3, M4, M5, M6, M8.
    - [x] M1 Deploy API contract: `docs/deploys-contract.md` (final), `docs/events.md`; typed
          `POST /deploys`, `GET /deploys`, `GET /deploys/{id}` returning 501; deploy events as run steps
          with a typed `run_steps.data` payload under their own SSE names; `runs.state` with
          `DeployRecord` for Medic (`app/pipeline/state.py`); `stop_reason = no_progress`; one migration;
          422s never echo env values
    - [x] M2 Sandbox (`app/sandbox/`): `DockerSandbox` build / run_smoke / push / cleanup over the Docker
          CLI (dind over TLS); smoke containers with no capabilities, no-new-privileges, 1 CPU / 512 MB /
          256 pids, tmpfs /tmp, env never in argv; pushes to `kind-registry:5000` and ECR (token from
          `aws_deploy_session()`, temp `DOCKER_CONFIG`, `--password-stdin`); `NebiusSandbox` stub
          (`TODO(nebius-sandbox)`). Live: `legacy-flask-app` naive build fails on `pg_config`, fixed build
          passes smoke and pushes to kind
    - [x] M3 Targets (`app/targets/`): `K8sTarget` and `EcsTarget` behind one protocol (discover, plan,
          preflight, apply, verify, logs, rollback_plan, rollback). Nothing leaves the sandbox before
          approval: the push is the first apply step (ECR repository first on ECS). `apply()` and
          `rollback()` need an `ApprovedAction` (`app/policy/actions.py`) bound to run + plan hash,
          signed with `APPROVAL_TOKEN_KEY` and spent once in the database (`approvals.used_at`), so
          it works across workers and restarts; rollback (also deleting a first deploy) needs an
          emergency approval. k8s: gc-<app> -> RoleBinding -> Secret -> Deployment -> Service ->
          Ingress; ECS: deploy role only, deploy_infra names and tags, task definitions never
          deregistered, SG source `GC_INGRESS_CIDR`. Live on kind (deploy, redeploy, rollback,
          delete) and ECS (legacy-flask-app deployed, verified, deleted by an emergency rollback,
          `cleanup-deploys RUN=` removed the rest; about $0.001)
    - [x] M7 Deploy page (`apps/web`): source picker (GitHub connection, repo, ref), target picker (kind / AWS
          deploy tier; observe tier disabled with "Turn on deploys"), env rows with a secret toggle,
          allow_missing_env; live view at `/deploy/[id]` on the shared run timeline (stage strip, build
          attempts with diff / error / model badge / tokens / cost, plan approval card with secrets masked and
          approve blocked on a failed preflight, verify, live URL with "View on Map", rollback card); deploys
          list. Runs on the M1 types; while `/deploys` answers 501, `lib/deploys-fake.ts` (deleted in M6)
          simulates the run in the browser behind a "Preview" banner
          Review fixes (2026-10-12): no `push` stage, the image push is apply's first step (plan
          `source_image`, resource action `push`); missing env fixed on the approval card (Set value /
          Continue without it re-runs only preflight, same image; `POST /deploys/{id}/env` lands in M6);
          disabled Approve explains the failing check; full repo name; preview on a simulated clock
- [ ] Phase 5 — Medic + fixes
- [ ] Phase 6 — Hardening + hosting
- [ ] Phase 7 — Submission
- [ ] Phase 8 — Judging upkeep

**Current phase:** 4 (Build + deploy agents, in progress, Rizwan); Phase 5 (Medic, Ahmed) in parallel;
Phase 3 complete 2026-10-09
**Last updated:** 2026-10-12
**Known issues / blockers:**
- Phase 0 open items: Tavily API key; budget alerts (AWS $20 / $50, Azure $20).
- Any member of a workspace can approve. Roles (who may approve write vs emergency actions) come with
  Settings in Phase 6.
- The browser opens the run stream directly on the API, so the hosted API needs `WEB_ORIGIN` set to the
  web app's URL and must be reachable from the browser (Phase 6).
- Spend caps (llm_usage) are checked without a lock. Exact with one worker; with several workers or
  concurrent calls the overshoot is at most one call each. `app/llm/evidence.py` (Medic, not wired yet) runs
  calls in parallel and needs per-call reservations before it is used.
- Probe results (2026-10-07, `scripts/probe_token_factory.py`): all three tiers work with the configured IDs; tool
  calls parse on every tier with reasoning on and off; `off` gives zero thinking tokens; `low` cuts thinking on
  Nano and Super but not on Ultra; the trace comes back in `reasoning` (Nano) or `reasoning_content` (Super,
  Ultra); `json_schema` returned valid JSON but free-text casing varied; no rerank model; the only embedding model
  is Qwen/Qwen3-Embedding-8B (4096 dims); Nano's first call took 20.6 s (likely a cold start).
- Follow-up probe (2026-10-07, `scripts/probe_followup.py`): strict `json_schema` is enforced but forces a wrong
  value inside the enum when nothing fits; Nano answers wrong from ~20k tokens (right at 6k), Lightning right at
  60k; embeddings honour `dimensions=1024`; Lightning ran a 3-turn tool loop exactly right at ~270 ms; Ultra low vs
  on is 311 vs 362 tokens on average with a wide spread. Details and asks for Nebius / NVIDIA: `FEEDBACK.md`.
- Still unverified: the default prices (third-party listing) and Lightning's price (none found, so its routes
  stay off).
- Tool calling with reasoning off is verified only on the Echo Ops toy agent. Re-check quality when
  Scout, Builder, and Deployer land.
- Phase 3 leftovers: the Azure connector runs on fixtures only (`TODO(azure-live)`);
  the summary check is rule based and does not catch negative claims (e.g. "does not depend on the ECS
  services" despite a `calls` edge); Medic's log and metric reads (`logs:FilterLogEvents`,
  `cloudwatch:GetMetricData`) are allowed but not yet called live through the connect role.
- Phase 5 runs in parallel with Phase 4. Medic needs only `Target.rollback()` from Phase 4 (M3); the
  shape of a finished deployment in `runs.state` is fixed (`DeployRecord`, `docs/deploys-contract.md`).
- The demo runs locally only; nothing in Phase 1 targets Neon. Run `make migrate DB_URL=<neon url>` when hosting.

## 15. Decisions log (newest first)

| Date | Decision | Why |
|---|---|---|
| 2026-10-12 | Before approval a deploy only builds and smoke-tests in the sandbox. Pushing the image (and creating the ECR repository) is the first step of apply, on kind and ECS alike; there is no `push` stage | Approval comes before anything leaves the sandbox; one rule for both targets (owner's M7 review) |
| 2026-10-12 | A missing required env var can be fixed on the approval card: set the value or continue without it. Only preflight re-runs and the plan is re-rendered with the same built image | No rebuild for a config fix; the approval stays on the exact plan |
| 2026-10-09 | A Vercel `calls` edge is high only when a plain env value matches the target's IP or endpoint; an encrypted value gives medium even when a workflow is seen writing the task IP into it | We never read the value, so we cannot confirm it still matches the current task |
| 2026-10-09 | Scout's summary is checked against the graph by rules (`check_summary`) and unsupported bullets are dropped and recorded as run steps; Scout's prompt is unchanged (tweak proposed to Ahmed) | Nano invented links on the live run (app storing state in the tfstate bucket, shared images, deploy.yml provisioning infrastructure); `agents/` is owned by Phase 4 |
| 2026-10-09 | Tooling (state and lock stores, the template bucket, `groundcrew-*` roles, Resource Explorer) is tagged `role=tooling` in the digest and folded into a Tooling group on the Map | It is real infrastructure, but the app does not use it; showing it as app resources led to false summary claims |
| 2026-10-09 | When the clone fails, GitHub discovery reads `.github/workflows/*.yml` through the contents API (GET only, 512 KB cap) | `deployed_by` and `calls` evidence depends on workflow files; one failed clone should not remove those edges |
| 2026-10-07 | Scout is rules first: a detector value (with file:line evidence) beats the model, and each disagreement is recorded as a conflict | Strict enums force confident wrong answers; files are the ground truth |
| 2026-10-07 | A model value no cited line supports is not applied: the field stays unknown, listed in `low_confidence`, with the value in `unverified` | Applying it let invented values (a `DATABASE_URL` for a Next.js app) flow into Builder and Deployer; later steps can still see the suggestion |
| 2026-10-07 | Scout's confidence is computed (known key fields, conflicts, unsupported values), not taken from the model | Model self-reported confidence is not calibrated |
| 2026-10-07 | Repo tools return capped, sorted text with explicit truncation markers; secret files (.env, keys) are listed by name only and never read | Model-facing output must be small and deterministic: predictable token costs and stable FakeLLM tests |
| 2026-10-07 | `GitCloneSource` takes https URLs only and sends `GIT_CLONE_TOKEN` as a header through git's environment | No ssh/file/ext transports or option injection; the token never appears in a URL, argv, or error |
| 2026-10-07 | `pathspec` for .gitignore rules | Exact git semantics (negation, anchors, nested files) without shelling out to git for local paths |
| 2026-10-07 | `LLM_CONTEXT_*` is the *usable* context (Nano 8,000, Lightning 64,000); bigger prompts go to the route's fallback tier | Probe: Nano accepted 65k tokens but answered wrong from ~20k; Lightning was right at 60k |
| 2026-10-07 | tool_step, investigate_step (fallback Super) and verify_recovery (fallback Nano) move to Lightning, reasoning off, automatically once `PRICE_LIGHTNING_*` is set | Lightning ran a tool loop exactly right at Nano-like latency; it is never called while its cost cannot be counted |
| 2026-10-07 | Ultra and Lightning never use reasoning `low` | It saved little (Ultra) or cost more (Lightning 603 vs 460 tokens) |
| 2026-10-07 | Every enum in a schema must allow `other` (the client refuses schemas without it); prompts allow `other` / null | Strict json_schema forces a value inside the enum even when none fits (a Rust repo came back as python / javascript) |
| 2026-10-07 | `FEEDBACK.md` in the repo root records Token Factory / Nemotron findings as we hit them | Feedback for the hackathon, Nebius, and NVIDIA, with numbers |
| 2026-10-07 | **AI agents never merge and never touch `main`.** Agents may create, commit, and push their own branches, open and update draft PRs, and rebase or fix conflicts on their own branches. They may not merge any PR (into `main` or any other branch), push or force-push to `main` or change it in any way, mark PRs ready for review, approve them, or enable auto-merge. If work seems to need a merge to continue (e.g. stacked branches), the agent stops and asks. | The project owner reviews and merges everything |
| 2026-10-07 | Ultra never runs with reasoning `low`; `propose_fix` is Ultra/on | Probe: on Ultra, `low` used more completion tokens than `on` (327 vs 218) |
| 2026-10-07 | Every schema field the code compares on is a lowercase Literal / StrEnum with `other`; model output is normalized before validation | Probe: valid JSON, but "TypeScript" / "JavaScript" / "javascript" for the same answer; strict enforcement is unproven |
| 2026-10-07 | Embeddings: Qwen/Qwen3-Embedding-8B, `dimensions=1024` requested, else the first 1024 values re-normalised; column stays 1024 | Only embedding model on Token Factory; it is a Matryoshka model, so a prefix is a valid embedding and no migration is needed |
| 2026-10-07 | Evidence ranking by embedding cosine similarity; rerank only when `MODEL_RERANK` is set | Token Factory has no rerank model |
| 2026-10-07 | Nemotron 3.5 Lightning registered as tier `lightning` (escalates to Super), no routes yet | Built for tool-call execution; route to it only after the follow-up probe measures latency, context, and tool loops |
| 2026-10-07 | Per-task routes (tier + reasoning mode) replace `LLM_REASONING_TIERS` | Reasoning depends on the job, not the model size: Scout and Dockerfile writing need a little, fixing a build needs it all, tool steps need none |
| 2026-10-07 | USD caps: $0.40 per run, `LLM_DAILY_USD_CAP` (2.00 dev, 0.50 hosted), `LLM_TOTAL_USD_CAP=25` as the hard stop, all counted in a `llm_usage` table | About $30 of credits must last through judging (Dec 15); in-memory counters would reset and not be shared |
| 2026-10-07 | Default prices from a third-party listing, marked unverified; a model with no price is never called | Spend is always counted; real prices go in `PRICE_*` once checked on the pricing page |
| 2026-10-07 | Budget stops keep `stop_reason=budget` with the exact cap in `runs.error`; `no_progress` comes with Builder (Step 5) | One stop reason for the UI to badge, with the detail where people read it |
| 2026-10-07 | Phase 4 API fields are optional in the OpenAPI schema until the web app uses them; only generated web files change (`docs/api-changes.md`) | Phase 3 and the frontend work in parallel; hand-written web code is theirs |
| 2026-10-07 | Reuse `run_steps.model_tier` and `cost_usd`; add `reasoning_mode`, `ultra_equiv_usd`, `fell_back`, `escalated`, `reasoning_excerpt` | No duplicate columns; the web app already reads `model_tier` |
| 2026-10-07 | FakeLLM subclasses the real client and replaces only the HTTP calls | Tests exercise the real routing, budget, repair, and accounting code without a key or network |
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
