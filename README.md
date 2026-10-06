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
cp .env.example .env    # then fill it in (see "Sign-in setup" below)
make install            # install API and web dependencies locally
make dev                # postgres (host port 5433), api :8000, worker, web :3000
make migrate            # in another terminal: create or upgrade the tables
```

Open http://localhost:3000 and sign in with GitHub. Your first sign-in creates your workspace.
Local dev always uses the compose Postgres, even if `.env` has a Neon `DATABASE_URL`.
`make models` sends one tiny test call to each configured Nemotron model.

### Sign-in setup

Groundcrew signs in with the OAuth client of our GitHub App.

1. In the GitHub App settings (Settings > Developer settings > GitHub Apps > your app):
   - Add the callback URL `http://localhost:3000/api/auth/callback/github`.
   - Under Permissions > Account permissions, set Email addresses to Read-only (optional; without it the
     email is left empty).
   - Copy the Client ID, and generate a client secret.
2. In `.env`:
   - `GITHUB_APP_CLIENT_ID` and `GITHUB_APP_CLIENT_SECRET` (or `AUTH_GITHUB_ID` / `AUTH_GITHUB_SECRET`
     to use a separate OAuth app).
   - `AUTH_SECRET`: `openssl rand -base64 33`
   - `INTERNAL_JWT_SECRET`: `openssl rand -base64 48`. The web server signs a 5-minute token with it for
     every API call; the API verifies it. It never reaches the browser.
   - `WEB_ORIGIN=http://localhost:3000` and `NEXT_PUBLIC_API_URL=http://localhost:8000`.
3. `make dev`, open http://localhost:3000, and continue with GitHub.

### Try the toy agent

Echo Ops is a test agent on Nemotron Nano with fake tools. Restarting a service needs approval.

1. On Overview or Runs, choose New run and start it with the suggested goal.
2. The run page shows each step as it happens. At `restart_fake_service` the run waits with an inline
   approval card.
3. Approve it there, or from Approvals. The run restarts the fake service, answers, and shows you as
   the decider. Refreshing the page mid-run picks the stream up where it left off.

A run uses about 1,700 tokens on Nano.

### Tests and other commands

```bash
make db       # start only Postgres
make test     # pytest on a real Postgres and vitest; the LLM is faked, no secrets needed
make lint     # ruff, mypy, eslint, tsc
make api-types  # regenerate the web app's typed API client after changing API schemas
make worker   # run the worker on the host instead of in compose
make keygen   # new Fernet key for SECRETS_KEY
make seed     # dev user + "Demo" workspace, for API experiments without the web app
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

The web app (Next.js) signs users in with GitHub through Auth.js. Its server proxies REST calls to the API
under `/api/gc`, attaching a short-lived internal JWT for the signed-in user. The live run stream goes from
the browser straight to the API with a 60-second token for that run.

Full design: [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md).

## Demo

TODO

## Hackathon notes

TODO

## License

License: MIT. See [LICENSE](LICENSE).
