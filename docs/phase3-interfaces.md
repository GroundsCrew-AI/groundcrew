# Interfaces Phase 3 plugs into

For the teammate building Phase 3 (connections, discovery, graph). Phase 4 code depends only on these small
interfaces, each with a temporary version so Phase 4 works before Phase 3 lands. Swap in the real
implementation without changing Phase 4 code.

## `RepoSource` (`apps/api/app/repos/source.py`)

Where a repository's files come from.

```python
class RepoSource(Protocol):
    def materialize(self) -> Path: ...   # a local directory with the working tree (no .git)
    def describe(self) -> str: ...       # one line for the timeline
    def cleanup(self) -> None: ...       # delete anything materialize() created; safe to repeat
```

Sources are also context managers: `with source as root: ...` always cleans up. `materialize()` and
`cleanup()` block; call them with `asyncio.to_thread` from async code.

Implementations now:
- `LocalPathSource(path, subpath=None)`: a directory on this machine, used as is, never deleted.
- `GitCloneSource(url, ref=None, subpath=None)`: shallow clone of a public `https://` URL into a temp dir.
  Optional `GIT_CLONE_TOKEN` is sent as an `Authorization` header through git's environment.

To add in Phase 3: `GitHubAppSource(installation_id, repo, ref)`. Mint an installation token with the
GitHub App, then either reuse `GitCloneSource` with that token (pass a `Settings` copy with
`git_clone_token` set) or download the tarball. Keep the same rules: working tree only, size cap
(`REPO_MAX_BYTES`), always clean up, never put the token in a URL or an error.

The repo tools (`list_files`, `read_file`) read from `ToolContext.repo_root`, the path `materialize()`
returned.

## Scout's system summary input (`apps/api/app/agents/scout/summary.py`)

`summarize_system(llm, budget, resources)` takes a plain list of `ResourceSummary`:

```python
class ResourceSummary(BaseModel):
    provider: str              # aws | azure | gcp | k8s | vercel | vm
    type: str
    name: str
    region: str | None = None
    relations: list[str] = []  # names of resources this one calls, reads, or is deployed by
    notes: str | None = None
```

The graph module can map its `resources` and `edges` rows into this. Scout sorts them, keeps the prompt under
about 6k tokens (Nano's reliable range), and says how many were left out.

## Coming with later steps

- `CredentialsProvider` (Step 6): `kubeconfig(connection_id) -> Path`, `aws_session(connection_id) -> boto3.Session`.
  Temporary: `EnvCredentialsProvider` reading env.
- `ResourceSink` (Step 6/7): `register_deployment(workspace_id, deployment)`. Temporary: a no-op logger; the
  graph module implements it so new deployments appear on the map.
- The shape of a finished deployment in `runs.state` (Step 8), which Medic reads for "recent deploys" and rollback.

---

# Phase 3: what landed (connections, discovery, graph)

For Phase 4 (Deployer) and Phase 5 (Medic). Everything below is in `main` once
`phase3-discovery` merges.

## Resource and Edge (tables `resources`, `edges`)

```
Resource: id, workspace_id, connection_id, provider (aws|azure|gcp|k8s|vercel|vm|github),
          type ("<service>:<kind>", e.g. ecs:service, rds:db, vercel:project, github:workflow),
          external_id (stable: ARN, Azure ID, "vercel:project:<id>", "github:repo:<owner/name>"),
          account, region, name, tags, config (non-secret JSON), coverage
          (observed|diagnosable|actionable), iac_path ("infra/aws/ecs.tf#L94"), repo,
          stale (not seen by the last complete discovery of its connection), last_seen_at
Edge:     id, workspace_id, from_id, to_id, kind, confidence (high|medium|low),
          evidence {text, rule, ...details}
```

Unique key: `(workspace_id, provider, external_id)`, so ids survive rediscovery. Edges are
recomputed for the whole workspace on every discovery from non-stale resources.

Edge kinds: `calls, reads, uses, for, pulls_from, logs_to, built_from, deployed_by, assumes,
defined_in, same_repo, related, runs_in, runs_as, routes_to, deployment_of`.

| Rule (`evidence.rule`) | Edge | Confidence |
|---|---|---|
| `sg_db_ingress` | service/function -> db `reads` (db SG allows the db port from the service's SG) | high |
| `task_secret` | service -> secret `uses` (task definition `secrets` block, by ARN) | high |
| `secret_db` | secret -> db `for` (Terraform secret version built from the db) / by name | high / medium |
| `secret_db` | service -> db `reads` via a secret for the db, no SG rule seen | medium |
| `image_ecr` | service -> ECR repo `pulls_from` (image URI) | high |
| `image_commit` | service -> GitHub repo `built_from` (SHA tag is a recent commit; `details.commit_url`) | high |
| `awslogs` | service/function -> log group `logs_to` | high |
| `terraform` | resource -> GitHub repo `defined_in` (resolved Terraform name matches) | high |
| `oidc_deploy` | service -> workflow `deployed_by` (role trusted only by the repo may UpdateService on it); workflow -> role `assumes` | high |
| `workflow_ecs` | service -> workflow `deployed_by` (service named in the job, or `vars.ECS_API_SERVICE` name hint) | high / medium |
| `vercel_job` | vercel project -> workflow `deployed_by` (linked repo, project id in the workflow, same name) / only project | high / medium |
| `env_url` | vercel project -> service/db `calls` (plain *_URL value host = task IP or endpoint) / env name + shared repo | high / medium |
| `same_repo` | vercel project -> service `same_repo` | low |
| `shared_tags` | resource -> resource `related` (specific tag shared by 2 to 5 resources) | low |
| structural | `runs_in` cluster, `runs_as` role, `deployment_of`, k8s `routes_to` | high |

## Connector interface (`app/discovery/types.py`)

Each provider is an async function `(Connection, Settings, Progress) -> ConnectorResult`
registered in `app/discovery/engine.py` (`DEFAULT_CONNECTORS`). `ConnectorResult` carries
`resources: list[DiscoveredResource]`, `repos: list[RepoFacts]` (Terraform, workflows,
Dockerfiles, compose, env names, commits, runs), `notes`, and `truncated`. `Progress(message)`
writes a run step. Clients:

- AWS `app/connectors/aws.py`: `AwsSessions.assume(role_arn, external_id)` returns a boto3
  session (cached in memory, never stored) whose clients refuse any operation outside
  `AWS_READ_OPERATIONS`; `AwsReader(session).call/paginate` only in `DISCOVERY_REGIONS`. Medic
  should add the reads it needs (e.g. `logs:FilterLogEvents`, `cloudwatch:GetMetricData`, both
  already allowed) through `AwsReader`, never a raw client. This is the `CredentialsProvider.
  aws_session(connection_id)` from "Coming with later steps": `get_sessions().assume(...)` with
  the connection's `role_arn` and `external_id`.
- GitHub `app/connectors/github.py`: `GitHubApp.get(installation_id, path)` (GET only) and
  `GitHubAppSource.create(app, installation_id, "owner/name", ref=...)`, the RepoSource above.
- Vercel `app/connectors/vercel.py`: `VercelClient(token, team_id)`; values only for plain
  *_URL / *_HOST.
- Azure and Kubernetes: fixture readers only (`TODO(azure-live)`, `TODO(k8s-live)`).

## Discovery contract

- Start: `POST /workspaces/{id}/discover` (all connections) or `POST /connections/{id}/discover`
  (one). Returns a run (`agent = "discovery"`); progress streams on its SSE like any run.
  Job kinds `discover_workspace` / `discover`, `max_attempts = 1`.
- Read: `GET /workspaces/{id}/graph?provider=&type=&include_stale=` and `GET /resources/{id}`
  (config, edges with evidence, recent deploys).
- `workspaces.settings.system_summary = {text, risks, generated_at, run_id, error}`.
- Coverage: `app.graph.coverage.register_actionable("ecs:service")` from a target adapter makes
  that type `actionable` on the next discovery. Nothing registers yet.

## For Medic: where to look

Start from the resource the incident names, then follow edges by kind: `reads`/`uses`/`for`
(data path and its secret: compare the secret's `last_changed_at` with the incident start),
`logs_to` (log group to query), `deployed_by` + `built_from` (recent runs and the commit of the
running image), `calls` (upstream callers that break too). `config.running_tasks`,
`config.deployments`, and `config.events` on ECS services hold stopped reasons and rollout
state from the last discovery; call AWS again for anything newer.

## Added minimally to existing code (noted as asked)

- `app/agents/scout/summary.py`: optional `risks`, own route `Task.summarize_system` (Nano,
  reasoning off, 1,500 tokens); the input is the digest from `app/graph/digest.py`.
- `app/worker.py`: handlers for `discover` and `discover_workspace`.
- No change to `GitCloneSource`: `GitHubAppSource` passes the installation token through a
  settings copy (`git_clone_token`), as suggested above. The token reaches git as
  `http.extraHeader` through git's environment, which is `git -c http.extraHeader` without
  putting the token in argv.
