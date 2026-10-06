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
| DB | Postgres (Neon) + SQLAlchemy 2 + Alembic + pgvector |
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

Env vars (exact model IDs must be copied from the Token Factory docs):
```
TOKEN_FACTORY_BASE_URL=
TOKEN_FACTORY_API_KEY=
MODEL_NANO=
MODEL_SUPER=
MODEL_ULTRA=
MODEL_EMBED=          # NVIDIA embedding model if available
TAVILY_API_KEY=
DATABASE_URL=
SECRETS_KEY=
GITHUB_APP_ID=
GITHUB_APP_PRIVATE_KEY=
DAILY_TOKEN_BUDGET=
MAX_ULTRA_CALLS_PER_INCIDENT=
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

`users`, `workspaces`, `connections`, `resources`, `edges`, `runs`, `run_steps`, `approvals`, `incidents` (with `embedding`), `events` (normalized telemetry timeline), `jobs`.

## 10. Main API

| Method | Path | Purpose |
|---|---|---|
| POST | `/connections` | Add AWS / Azure / GitHub / cluster / VM connection |
| POST | `/workspaces/{id}/discover` | Start discovery |
| GET | `/workspaces/{id}/graph` | Resources + edges |
| POST | `/deploys` | Scout → Builder → Deployer |
| POST | `/incidents` | Open incident, start Medic |
| GET | `/runs/{id}/events` | SSE stream of steps |
| GET | `/approvals` | Pending approvals |
| POST | `/approvals/{id}/decide` | Approve / reject |
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
- [ ] Phase 1 — Backend + agent loop
- [ ] Phase 2 — Frontend shell
- [ ] Phase 3 — Discovery + graph
- [ ] Phase 4 — Build + deploy agents
- [ ] Phase 5 — Medic + fixes
- [ ] Phase 6 — Hardening + hosting
- [ ] Phase 7 — Submission
- [ ] Phase 8 — Judging upkeep

**Current phase:** 0
**Last updated:** 2026-10-05
**Known issues / blockers:** none yet

## 15. Decisions log (newest first)

| Date | Decision | Why |
|---|---|---|
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
