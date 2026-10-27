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

Open http://localhost:3000 and create an account (email and password, or Google or GitHub when
configured). Your account gets a workspace of its own; invite your team from Settings.
Settings, Notifications sends incidents and approval requests to a Slack channel (incoming
webhook) and by email; see "Notifications" in [docs/self-hosting.md](docs/self-hosting.md).
Local dev always uses the compose Postgres, even if `.env` has a Neon `DATABASE_URL`.
`make models` sends one tiny test call to each configured Nemotron model.

### Sign-in setup

Email and password sign-up works with no extra setup: in dev, the API prints the confirmation email
(with its link) to its terminal. For real email set the `SMTP_*` settings; to offer Google sign-in set
`AUTH_GOOGLE_ID` / `AUTH_GOOGLE_SECRET` (callback `http://localhost:3000/api/auth/callback/google`). See
"Accounts and sign-in" in [docs/self-hosting.md](docs/self-hosting.md).

GitHub sign-in uses the OAuth client of our GitHub App:

1. In the GitHub App settings (Settings > Developer settings > GitHub Apps > your app):
   - Add the callback URL `http://localhost:3000/api/auth/callback/github`.
   - Under Permissions > Account permissions, set Email addresses to Read-only (recommended: Groundcrew
     then knows which addresses GitHub verified, so email invites and account linking work).
   - Copy the Client ID, and generate a client secret.
2. In `.env`:
   - `GITHUB_APP_CLIENT_ID` and `GITHUB_APP_CLIENT_SECRET` (or `AUTH_GITHUB_ID` / `AUTH_GITHUB_SECRET`
     to use a separate OAuth app).
   - `AUTH_SECRET`: `openssl rand -base64 33`
   - `INTERNAL_JWT_SECRET`: `openssl rand -base64 48`. The web server signs a 5-minute token with it for
     every API call; the API verifies it. It never reaches the browser.
   - `WEB_ORIGIN=http://localhost:3000` and `NEXT_PUBLIC_API_URL=http://localhost:8000`.
3. `make dev`, open http://localhost:3000, and create an account or continue with GitHub.

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

## Connecting your cloud

Groundcrew reads with scoped access. It never sees secret values or your data, and it never stores AWS
keys. Deploying is opt-in per AWS account (the Deploy tier below).
Open **Connections** in the sidebar and add what you use. Each one takes a minute or two.

> Screenshot placeholder: the Connections page, with AWS, GitHub, and Vercel cards.

### AWS

1. Click **Connect AWS**. A new tab opens in the AWS console with a CloudFormation stack that is
   already filled in: the stack name (`groundcrew-read-only`), your workspace's ExternalId, and the
   Groundcrew identity that may use the role. If AWS asks you to sign in, sign in to the account you want
   to connect.
2. Check that the region at the top right is **US East (Ohio) us-east-2**. At the bottom, tick
   *I acknowledge that AWS CloudFormation might create IAM resources with custom names*, then click
   **Create stack**.
3. Wait about a minute for **CREATE_COMPLETE**. Open the **Outputs** tab and copy **RoleArn**.
4. Back in Groundcrew, paste it and click **Verify**. Groundcrew checks that the role refuses access
   without your ExternalId and works with it, then shows the account as connected.

> Screenshot placeholder: the CloudFormation Quick-create page with the parameters filled in.
>
> Screenshot placeholder: the stack's Outputs tab with RoleArn.

The stack creates one read-only role and a Resource Explorer index. The role is explicitly denied secret
values, SSM parameters, KMS decrypt, and S3 objects. To disconnect, remove the connection in Groundcrew and
delete the `groundcrew-read-only` stack in CloudFormation.

If Verify says the ExternalId is different, the stack was made from an older link: delete the stack and
click **Connect AWS** again.

**Observe or Deploy.** The AWS card has a choice. **Observe** (the default) is the read-only role above.
**Deploy** also creates an ECS cluster `groundcrew-deploys` and a `groundcrew-deploy` role that may only
create and change Groundcrew's own resources (named `groundcrew-*`, tagged `project=groundcrew`, logs
under `/groundcrew/`, secrets under `groundcrew/`). It can write a deploy's secrets but is explicitly
denied reading any secret value; only ECS reads them, when it starts the app. For an account that is already
connected, update its stack with `Tier=deploy` and click **Enable deploys**. Exact permissions:
[docs/phase4-infra.md](docs/phase4-infra.md).

### GitHub

1. Click **Pick repositories**. The first time, click **Install the GitHub App**, choose your account,
   and select the repositories Groundcrew may read.
2. Come back, click **I installed it**, tick the repositories, and click **Link**.

> Screenshot placeholder: the repository picker.

### Vercel

1. At vercel.com/account/tokens, create a token with your team as its scope.
2. Click **Add token**, paste it, and add your Team ID (Vercel, Settings, General; it starts with
   `team_`). Groundcrew reads projects, domains, deployments, and the names of env vars, but never
   encrypted or sensitive values.

### Then

Click **Run discovery**. It streams what it finds and takes well under two minutes. Open **Map** to see
every resource, how they connect, and why Groundcrew thinks so.

Running Groundcrew yourself? See [docs/self-hosting.md](docs/self-hosting.md) for the one AWS identity
and the GitHub App it needs.

### Marking your own tooling

Some of what discovery finds manages or watches your app without being part of it: Terraform state
buckets and lock tables, CI roles, a Lambda that updates DNS, a cluster's Argo CD. Groundcrew puts
those in a collapsed **Tooling** group on the Map, leaves them out of the system summary, and does not
read them as neighbors when Medic investigates an incident (it still reads one an incident names
directly).

It recognizes some by name (state buckets, lock tables, its own `groundcrew-*` roles). Mark the rest
yourself:

| Where | Key | Value |
|---|---|---|
| AWS tag (Lambda, IAM role, log group, EventBridge rule, secret, anything taggable) | `groundcrew:role` | `tooling` |
| Kubernetes label | `groundcrew/role` | `tooling` |

The tag wins over the name rules. Set it to `app` to keep a resource in the app view when its name
looks like tooling (a bucket called `uploads-tfstate-export`, say). Values are case-insensitive;
anything else is ignored. Discovery reads the tags with the read-only role's existing permissions
(the inventory, `lambda:ListTags`, `iam:GetRole`, secret metadata), so the role needs no update. Run
discovery again after you tag.

## Repository layout

- `apps/api`: FastAPI control plane (Python 3.12, uv)
- `apps/web`: Next.js 15 frontend (TypeScript, Tailwind, shadcn/ui)
- `scripts`: developer scripts, including the Token Factory smoke test and `replay_discovery.py`
  (discovery on recorded fixtures)
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

Discovery is a run like any other (`agent = "discovery"`), so it streams over the same SSE. It reads each
connection with read-only credentials, upserts resources by a stable external id (anything not seen again is
marked stale), infers edges with a confidence and one line of evidence each, and asks Scout for a system
summary that is then checked against the graph. The **Map** page draws that graph. Contracts for the agents
that build on it: [docs/phase3-interfaces.md](docs/phase3-interfaces.md).

More design notes are in [docs/](docs/).

## Demo

- **Live:** [groundkrew.com](https://groundkrew.com). Sign in with GitHub. The **Example workspace** shows a
  real workspace's map, incidents and deploys (scrubbed of account details) without connecting anything; to
  try it on your own infrastructure, connect AWS read-only, GitHub or Vercel under Connections.
- **Video:** VIDEO_LINK (under 3 minutes: connect and map, a deploy that fixes its own build in a sandbox, an
  incident from detection to resolution, and the model routing behind it).

## How Groundcrew uses NVIDIA Nemotron on Nebius Token Factory

Every model call goes through Nebius Token Factory's OpenAI-compatible API, in one client
(`apps/api/app/llm/client.py`): retries, fallback, JSON-schema output with one repair, and the token count and
cost of every call stored with the run step. Each step of each agent asks for the Nemotron 3 size it needs
(`apps/api/app/llm/router.py`):

| Model | Steps | Why this size |
|---|---|---|
| **Nemotron 3 Nano** | Scout reads repositories (framework, port, health check), summarizes the discovered system, compresses evidence, classifies signals, checks recovery | Fast and cheap for reading and short structured answers |
| **Nemotron 3 Super** | Builder writes and fixes Dockerfiles, Deployer plans the target config, Medic's tool steps and confirming a remembered diagnosis | Reliable structured output and code edits at a fraction of Ultra's price |
| **Nemotron 3 Ultra** | Medic's root cause and fix proposals; any step that escalates | The hardest cross-system reasoning |

- **Escalation:** when a smaller model fails a step, the step moves up one size with full reasoning and more
  tokens. For example, the Builder's fourth build attempt goes to Ultra; the demo shows a build Super could
  not fix passing on Ultra.
- **Reasoning per step:** each route sets Nemotron's reasoning mode (off, low effort or on), so short steps
  don't pay for long thinking.
- **Incident memory:** past incidents are embedded with Qwen3-Embedding-8B (also on Token Factory) so Medic can
  reuse a diagnosis when a new incident is a close match, and evidence is ranked by similarity.
- **Cost is visible and capped:** every call shows its model, tokens and dollar cost in the run timeline, with
  the "Ultra-only" cost beside it, and daily spend caps stop runaway loops (`apps/api/app/llm/budget.py`). In the
  demo, a full incident investigation used 34,633 tokens for about $0.01, and a deploy with an automatic build
  fix cost $0.0075.

**Where Token Factory helped:** one API key and one endpoint serve all three Nemotron sizes and the embedding
model, so routing a step to a different size is a configuration change, not a new integration. Per-token
pricing let us show the cost of every call to users and set budgets, and we ran no GPUs of our own. What we
measured along the way, good and bad, is in [FEEDBACK.md](FEEDBACK.md).

## Hackathon notes

- **Track:** Coding and Agentic Engineering. Groundcrew's agents write, run and test code: the Builder writes
  a Dockerfile, builds and smoke-tests it in a sandbox, reads the error and patches it until it passes.
- **Also used:** [Tavily](https://tavily.com) for web research in Medic's investigation of new incidents
  (`apps/api/app/tools/tavily.py`), used as background, never as evidence.
- **Built during the hackathon:** the repository starts on 2026-10-06, inside the Submission Period.
- **Feedback** on Token Factory and Nemotron: [FEEDBACK.md](FEEDBACK.md).

## License

License: MIT. See [LICENSE](LICENSE).
