# Groundcrew

Groundcrew is an AI DevOps team made of NVIDIA Nemotron agents. It connects to a customer's existing
infrastructure on any cloud (AWS, Azure, Kubernetes, VMs, Vercel), maps everything into one cross-cloud
resource graph, deploys apps, and investigates and fixes incidents.

**Built with NVIDIA models on Nebius Token Factory.**

## Prerequisites

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Node.js 22+ and npm
- Docker with Docker Compose
- make
- A [Nebius Token Factory](https://docs.tokenfactory.nebius.com/) API key

## Running locally

```bash
cp .env.example .env    # fill in TOKEN_FACTORY_* and MODEL_* values
make install            # install API and web dependencies locally
docker compose up --build   # postgres (host port 5433), api :8000, worker, web :3000
make migrate            # in another terminal: create the tables
make seed               # dev user + "Demo" workspace; prints the workspace id
```

Local dev always uses the compose Postgres, even if `.env` has a Neon `DATABASE_URL`.
`make models` sends one tiny test call to each configured Nemotron model.

### Try the toy agent

Echo Ops is a test agent on Nemotron Nano with fake tools. Restarting a service needs approval.

```bash
WS=<workspace id from make seed>

# Start a run (the worker picks it up)
RUN=$(curl -s -X POST localhost:8000/runs -H 'Content-Type: application/json' \
  -d "{\"agent\": \"echo_ops\", \"goal\": \"List the services and restart the one named api.\", \"workspace_id\": \"$WS\"}" \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["id"])')

# Watch steps arrive live (leave this running)
curl -N localhost:8000/runs/$RUN/events

# In another terminal: the run is waiting at restart_fake_service
curl -s 'localhost:8000/approvals?status=pending'
curl -s -X POST localhost:8000/approvals/<approval id>/decide \
  -H 'Content-Type: application/json' -d '{"decision": "approve"}'   # or "reject"

# The stream shows the restart and the final answer, then ends. Full record:
curl -s localhost:8000/runs/$RUN
```

### Tests and other commands

```bash
make db       # start only Postgres
make test     # pytest on a real Postgres; the LLM is faked, no secrets needed
make lint     # ruff, mypy, eslint, tsc
make worker   # run the worker on the host instead of in compose
make keygen   # new Fernet key for SECRETS_KEY
make down     # stop the stack
```

## Repository layout

- `apps/api`: FastAPI control plane (Python 3.12, uv)
- `apps/web`: Next.js 15 frontend (TypeScript, Tailwind, shadcn/ui)
- `scripts`: developer scripts, including the Token Factory smoke test
- `infra`: customer connection templates and our hosting setup
- `demo`: sample app and demo cloud environments

## Architecture

The API never runs agents itself. `POST /runs` writes a run and a job; the worker claims jobs from Postgres
(`FOR UPDATE SKIP LOCKED`) and runs the agent loop: call the model, send tool calls through the policy engine,
store every step in `run_steps`, repeat. Read tools run at once. Write and emergency tools create an approval
and pause the run; a decision enqueues a job that resumes it. `GET /runs/{id}/events` streams steps over SSE.

Full design: [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md).

## Demo

TODO

## Hackathon notes

TODO

## License

License: MIT. See [LICENSE](LICENSE).
