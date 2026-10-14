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

Full design: [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md).

## Demo

TODO

## Hackathon notes

TODO

## License

License: MIT. See [LICENSE](LICENSE).
