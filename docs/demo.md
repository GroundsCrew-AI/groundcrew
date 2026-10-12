# Phase 4 demo: GitHub to a live URL on kind and AWS (2 minutes)

One repository that has never been containerized, `GroundsCrew-AI/legacy-flask-app`, goes from
GitHub to a live URL on the local kind cluster and on AWS ECS, with an automatic build fix and an
approval before anything touches infrastructure. Then the Map shows both deployments.

**Status:** final (Phase 4 done, 2026-10-12). Every step below ran live through `POST /deploys`
on 2026-10-12 (`docs/deploy-live.md`, Phase 4 evidence): kind in 2.0 min and ECS in 4.9 min,
each with 2 build attempts (one automatic psycopg2 fix), about $0.003 of model calls each.

## The repository

`GroundsCrew-AI/legacy-flask-app` (public, default branch `main`; the Groundcrew GitHub App
installation on GroundsCrew-AI can read it). Its code is the same as
`demo/fixtures/py-build-trap/repo`, so Scout's golden spec there applies.

| Fact | Checked 2026-10-11 |
|---|---|
| No Dockerfile, no Procfile | Files: `app.py`, `requirements.txt`, `.env.example`, `.python-version`, `README.md`, `LICENSE` |
| Python 3.12, Flask, gunicorn, `psycopg2==2.9.9` | `.python-version`, `requirements.txt` |
| Start: `gunicorn app:app --bind 0.0.0.0:8000`, port 8000, health `/health` | Scout's golden spec (`demo/fixtures/py-build-trap/expected_appspec.json`) |
| `/health` works without a database and without `DATABASE_URL` | Built and run in a throwaway container with no database: `GET /health` returned 200 `{"status":"ok"}` |
| `/version` needs Postgres | Same container: 500 (`psycopg2.OperationalError`, no server). Not used by the demo |
| **The build trap** | `pip install -r requirements.txt` on `python:3.12-slim` fails: `pg_config executable not found` (`psycopg2` builds from source and needs libpq headers and a compiler) |

The automatic fixes that work (each verified in a container):

1. Install the build dependencies: `apt-get install -y --no-install-recommends gcc libc6-dev
   libpq-dev` (or `build-essential libpq-dev`). Note: `gcc libpq-dev` alone with
   `--no-install-recommends` still fails (`stdlib.h: No such file or directory`), so the Builder
   may take two attempts. That makes a good demo, not a problem.
2. Or use `psycopg2-binary==2.9.9` instead of `psycopg2==2.9.9` (a requirements patch in the
   sandbox; the repository itself is not changed).

### Env var defaults for the Deploy page

| Name | Value | Secret | Why |
|---|---|---|---|
| `DATABASE_URL` | none | yes | Scout marks it required (from `.env.example`), but the app starts and `/health` answers without it. Deploy with **allow missing env** checked, or give a dummy value such as `postgresql://app:unused@localhost:5432/app` as a secret |

Nothing else is read from the environment. gunicorn listens on 8000 (the start command binds it).

## Before the demo

```bash
make kind-up && make kind-kubeconfig && make kind-check   # local cluster (docs/kind.md)
make dev KIND=1                                           # api, worker, dind, web on the kind network
make kind-register LOGIN=<github login> DISCOVER=1        # kind as a Kubernetes connection
make dind-check                                           # the build path works (PR #13)
make cleanup-deploys HOURS=0                              # dry run (PR #13): nothing left over
```

- AWS: the demo account's connection (881050425883) has **Tier=deploy** (Connections page shows
  Deploy) and `make smoke-ecs CONNECTION=<id>` passes as a dry run.
- Open, signed in, one tab each: Deploy page, Map, Connections.
- Delete any earlier `gc-legacy-flask-app` namespace and `groundcrew-legacy-flask-app` ECS service
  (`make cleanup-deploys HOURS=0 APPLY=1`, only with the account owner's OK), so the run starts clean.
- Browser zoom 125%, dark theme, notifications off.

## Script

Times are cumulative. Build and deploy waits are cut in the video or filled with narration; on a
warm dind cache the kind deploy takes about a minute end to end.

| Time | Show | Say |
|---|---|---|
| 0:00 | Deploy page. Source: GitHub, `GroundsCrew-AI/legacy-flask-app`, `main`. Target: `kind groundcrew` | "A Flask app nobody ever containerized: no Dockerfile, no deploy config. We point Groundcrew at it and pick the local Kubernetes cluster." |
| 0:10 | Env vars: `DATABASE_URL` shown as secret, **allow missing env** checked. **Deploy** | "Scout found one env var. It's a secret, so its value never comes back to the browser." |
| 0:15 | Run timeline: Scout finishes (Nano badge, tokens, cost). AppSpec: Flask, port 8000, `/health` | "Scout read the repo with rules first and one small model call: Python 3.12, gunicorn on 8000, health check at /health." |
| 0:25 | Builder attempt 1 fails. Expand the error summary: `pg_config executable not found` | "The first build fails, like it would for a person: psycopg2 builds from source and the slim image has no Postgres headers." |
| 0:40 | Builder attempt 2 (or 3): expand the Dockerfile diff (adds `libpq-dev`, `libc6-dev`, `gcc`). Build passes, smoke test passes in the sandbox | "Builder reads the error, patches the Dockerfile in the sandbox, and rebuilds. No one edited anything." |
| 0:50 | Approval card: the rendered plan (namespace `gc-legacy-flask-app`, Deployment, Service, Ingress, image `kind-registry:5000/...`, `DATABASE_URL` masked). **Approve** | "Nothing touches infrastructure until a person approves the exact plan. Secrets stay masked." |
| 1:00 | Deployer: apply, verify passes. Live URL `http://legacy-flask-app.localhost`: click it, `{"status":"ok"}` | "Deployed and verified. That's a live URL on Kubernetes." |
| 1:10 | Back to Deploy. Same repo, target: `AWS 881050425883` (deploy tier). **Deploy** | "Same repo, now AWS." |
| 1:20 | Builder fails once on psycopg2 again and fixes it (the run starts from the repository; the cached layers make it quick). Approval card: the push to ECR is the first step after approval, then ECS service in `groundcrew-deploys`, Fargate 0.25 vCPU / 0.5 GB, security group on port 8000, log group, ECR repository, the secret by name only. **Approve** | "The plan is different, the guardrails are the same: only groundcrew-named, tagged resources, through a role that can't touch anything else." |
| 1:35 | Verify passes; live URL `http://<task IP>:8000/health` answers | "Live on Fargate." |
| 1:45 | **View on Map** from the live URL card: the `legacy-flask-app` Deployment (kind) and ECS service, each marked actionable with a deployed-by-Groundcrew edge (the rest of each stack appears after the next discovery) | "And Groundcrew knows what it just deployed: both show up on the Map, linked to the run that made them." |
| 1:55 | Deploys list: two rows, status, target, URL, duration, cost | "Two clouds, one repo, one fix, two approvals. About a cent of AWS." |

## If something goes wrong on stage

| Problem | Do |
|---|---|
| Build takes long (cold cache) | Narrate the error summary and the diff; the attempts are already on screen |
| No automatic fix needed (the cache has a working image) | Run `docker compose exec dind docker builder prune -af` before the demo, so the first build really fails |
| Approval card does not appear | Check the worker logs (`docker compose logs -f worker`); the run is paused, not lost |
| `http://legacy-flask-app.localhost` gives 404 | ingress-nginx needs a few seconds after apply; reload |
| ECS verify slow | Fargate pulls take 30 to 60 s; the smoke test's service was stable in 37 s |

## After the demo

Roll back the ECS deploy (an emergency approval: on a first deploy the service is deleted),
then clean up what that run left:

```bash
SELF_APPROVE=1 scripts/live_phase4.sh --rollback <ECS deploy id>   # dev stack only
make cleanup-deploys HOURS=0 RUN=<ECS run id>                       # dry run first
make cleanup-deploys HOURS=0 APPLY=1 RUN=<ECS run id>               # AWS writes: owner's OK
```

Cost of the AWS part: about $0.003 for 10 minutes at 0.25 vCPU / 0.5 GB with a public IP
(`docs/deploy-live.md`, Cost; the full price table comes with PR #13 in `docs/phase4-infra.md`).
