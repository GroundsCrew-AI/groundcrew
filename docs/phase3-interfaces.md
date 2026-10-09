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

- `CredentialsProvider` and `ResourceSink`: landed in Phase 3, signatures below.
- The shape of a finished deployment in `runs.state` (Step 8), which Medic reads for "recent deploys" and rollback.

---

# Phase 3: what landed (connections, discovery, graph)

For Phase 4 (Deployer) and Phase 5 (Medic). Everything below is in `main` once
`phase3-discovery` merges. Phase 3 changed no file under `llm/`, `repos/`, `agents/`,
`sandbox/`, `targets/`, or `tools/repo_read.py`.

## The five contracts (final signatures)

### 1. CredentialsProvider (`app/connectors/credentials.py`)

```python
class CredentialsProvider(Protocol):
    def kubeconfig(self, connection_id: uuid.UUID) -> Path: ...
    def aws_session(self, connection_id: uuid.UUID) -> boto3.Session: ...
    def azure_credential(self, connection_id: uuid.UUID) -> AzureCredential: ...

class AzureCredential(Protocol):          # azure-identity's TokenCredential shape
    def get_token(self, *scopes: str, **kwargs: Any) -> Any: ...
```

All three block: call them with `asyncio.to_thread`. Errors are `CredentialsError` (never
with a secret in the message).

- `ConnectionCredentialsProvider(connections, *, sessions=None)`, or
  `await ConnectionCredentialsProvider.for_workspace(session, workspace_id)`. Use it as a
  context manager (or call `close()`) so kubeconfig files are deleted.
  - `aws_session`: STS AssumeRole into the connection's `role_arn` with its `external_id`,
    from Groundcrew's platform identity (`GROUNDCREW_AWS_ACCESS_KEY_ID` /
    `GROUNDCREW_AWS_SECRET_ACCESS_KEY`, else the default chain without profiles or
    `~/.aws`). The session lasts at most an hour, is cached in memory only, and refuses any
    operation outside `AWS_READ_OPERATIONS`.
  - `kubeconfig`: decrypts the stored kubeconfig into a 0600 file in a 0700 temp dir.
  - `azure_credential`: a `ClientSecretAzureCredential` (tenant, client, decrypted secret);
    `get_token` raises until live Azure lands (`TODO(azure-live)`, swap for
    `azure.identity.ClientSecretCredential`).
  - Secrets stay Fernet-encrypted in `connections.secret_encrypted` and are decrypted only
    inside these calls.
- `EnvCredentialsProvider(env=None)`: the temporary version. Same credentials for every
  connection id: `KUBECONFIG`; `GROUNDCREW_DEV_AWS_ROLE_ARN` + `GROUNDCREW_DEV_AWS_EXTERNAL_ID`
  (assumed from the platform identity, like a real connection); `AZURE_TENANT_ID`,
  `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`.

### 2. GitHubAppSource (`app/connectors/github_source.py`), a RepoSource

```python
class GitHubAppSource:
    def __init__(self, installation_id: int, repo: str, ref: str | None = None, *,
                 subpath: str | None = None, app: GitHubApp | None = None) -> None: ...
    def materialize(self) -> Path: ...     # mints the installation token, then clones
    def describe(self) -> str: ...
    def cleanup(self) -> None: ...
    commit: str | None                     # HEAD of the clone, after materialize()
```

`materialize()` blocks; call it with `asyncio.to_thread`. It mints an installation token
(cached in memory until 5 minutes before expiry) and runs `GitCloneSource` unchanged with the
token on a settings copy, so every GitCloneSource rule holds: https only, shallow, working
tree only, `REPO_MAX_BYTES`, `GIT_CLONE_TIMEOUT_SECONDS`, temp dir always deleted. The token
reaches git only through git's environment (`GIT_CONFIG_KEY_0=http.<host>/.extraheader`),
the same setting as `git -c http.extraHeader` but never in argv, the URL, an error, or a log.
Also a context manager: `with GitHubAppSource(installation_id, "owner/name") as root: ...`.

### 3. ResourceSink (`app/graph/sink.py`)

```python
class ResourceSink(Protocol):
    async def register_deployment(self, workspace_id: uuid.UUID,
                                  deployment: Deployment) -> uuid.UUID: ...   # the resource id

class Deployment(BaseModel):
    resource: DiscoveredResource           # stable external_id (ARN, "k8s:<ns>/<kind>/<name>")
    connection_id: uuid.UUID | None = None # keeps it fresh on that connection's discovery
    run_id: uuid.UUID | None = None
    repo: str | None = None
    commit: str | None = None
    image: str | None = None
    deployed_by: str | None = None         # external_id of a resource already in the graph
    summary: str | None = None             # evidence line
    at: datetime = now
```

`GraphResourceSink(session_factory)` upserts the resource (same key as discovery, so ids
survive; `config.last_deployment` records run, repo, commit, image) and one `deployed_by`
edge to `deployed_by` or, by default, the workspace's "Groundcrew" node
(`groundcrew:deployer`). Discovery recomputes inferred edges on every run but keeps these
(evidence rule `groundcrew_deploy`). `NoopResourceSink` only logs.

### 4. Resource and Edge (`app/db/models.py`, `app/discovery/types.py`)

```
Resource: id, workspace_id, connection_id, provider (aws|azure|gcp|k8s|vercel|vm|github|groundcrew),
          type ("<service>:<kind>": ecs:service, rds:db, vercel:project, github:workflow, ...),
          external_id (stable: ARN, Azure ID, "vercel:project:<id>", "github:repo:<owner/name>"),
          account, region, name, tags, config (non-secret JSON), iac_path ("infra/aws/ecs.tf#L94"),
          repo, coverage (observed|diagnosable|actionable), stale, last_seen_at
Edge:     id, workspace_id, from_id, to_id, kind, confidence (high|medium|low),
          evidence {text, rule, ...details}
```

`Coverage`, `EdgeKind`, and `Confidence` are enums in `app/discovery/types.py`. Unique keys:
`(workspace_id, provider, external_id)` for resources, `(from_id, to_id, kind)` for edges.

A connector or a target's `discover()` returns a `ConnectorResult`:

```python
class ConnectorResult(BaseModel):
    resources: list[DiscoveredResource]
    edges: list[DiscoveredEdge]            # relationships it knows for certain
    repos: list[RepoFacts]                 # Terraform, workflows, Dockerfiles, commits, runs
    notes: list[str]
    truncated: bool

class DiscoveredEdge(BaseModel):
    from_external_id: str
    to_external_id: str
    kind: EdgeKind
    confidence: Confidence = Confidence.high
    evidence: str
    rule: str = "declared"
```

Declared edges are kept with the from-resource and survive rediscovery; the engine adds the
inferred ones below.

**Edge kinds.** Core: `calls`, `reads`, `deployed_by`, `defined_in`. **Extensions, flagged for
Ahmed** (each needed by a Done-when edge, and none maps onto a core kind without losing what
Medic needs):

| Kind | Meaning | Why not a core kind |
|---|---|---|
| `uses` | service gets a secret or config from (service -> Secrets Manager secret) | Medic compares the secret's `last_changed_at` with an incident; `reads` means data |
| `pulls_from` | service pulls its image from (service -> ECR repository) | a registry is neither data nor where the service is defined |
| `logs_to` | service sends logs to (service -> log group) | tells Medic which log group to query; it also makes a resource `diagnosable` |

Mapped onto core kinds: `built_from` (image tag is a commit) -> `defined_in` (one edge with the
Terraform evidence and `details.commit`, `commit_url`); Vercel `deployment_of` -> project
`deployed_by` its latest production deployment; Kubernetes `routes_to` -> `calls`. Removed
(their facts are evidence on other edges): `for` (secret -> db is `details.secret_db` on the
service -> db `reads` edge), `assumes` (the OIDC role is named in the `deployed_by` evidence),
`runs_in`, `runs_as`, `same_repo`, `related`.

| Rule (`evidence.rule`) | Edge | Confidence |
|---|---|---|
| `sg_db_ingress` | service/function -> db `reads` (db SG allows the db port from the service's SG) | high |
| `task_secret` | service -> secret `uses` (task definition `secrets` block, by ARN) | high |
| `secret_db` | service -> db `reads` via a secret for the db, no SG rule seen (`details.secret_db` says why: Terraform or names) | medium |
| `image_ecr` | service -> ECR repo `pulls_from` (image URI) | high |
| `terraform` | resource -> GitHub repo `defined_in` (resolved Terraform name matches) | high |
| `image_commit` | service -> GitHub repo `defined_in` (image tag is a recent commit; `details.commit_url`), merged into the `terraform` edge when there is one | high |
| `awslogs` | service/function -> log group `logs_to` | high |
| `oidc_deploy` | service -> workflow `deployed_by` (role trusted only by the repo may UpdateService on it) | high |
| `workflow_ecs` | service -> workflow `deployed_by` (service named in the job, or `vars.ECS_API_SERVICE` name hint) | high / medium |
| `vercel_job` | vercel project -> workflow `deployed_by` (linked repo, project id in the workflow, same name) / only project | high / medium |
| `env_url` | vercel project -> service/db `calls` (plain *_URL value host = task IP or endpoint) / env name + shared repo | high / medium |
| `vercel_deployment` | vercel project -> its latest production deployment `deployed_by` | high |
| `k8s_selector`, `k8s_ingress` | k8s service -> workload, ingress -> service `calls` | high |
| `workflow_file` | workflow -> GitHub repo `defined_in` | high |
| `declared` (or the connector's rule) | whatever a `DiscoveredEdge` says | as declared |
| `groundcrew_deploy` | resource -> deployer `deployed_by` (ResourceSink) | high |

### 5. Graph digest for Scout (`app/graph/digest.py`)

```python
def digest(resources: Sequence[DiscoveredResource], edges: Sequence[InferredEdge], *,
           risks: Sequence[Risk] = (), token_cap: int = 6_000) -> list[ResourceSummary]: ...
async def workspace_digest(session: AsyncSession, workspace_id: uuid.UUID, *,
                           token_cap: int = 6_000) -> list[ResourceSummary]: ...
def find_risks(resources, edges) -> list[Risk]       # Risk(resource: key, text: "Risk: ...")
```

The input to Scout's existing `summarize_system(llm, budget, resources)` (task
`summarize_graph`, Nano, unchanged): one `ResourceSummary` per resource, sorted by provider,
type, and name, with relations by name (high and medium edges) and facts in `notes`. Risks
found by rules (public database, security group open to the world, a service with no load
balancer or stable endpoint, a failed rollout, missing tasks) go into the `notes` of the
resource they are about. Under `token_cap` tokens by folding: resources with no relations
and no risks become one line per provider and type, then everything does. No ARNs, raw JSON,
or secrets reach the model. Discovery stores `workspaces.settings.system_summary = {text,
risks, generated_at, run_id, error}`.

### Summary safety (our side) and a proposed prompt tweak for Ahmed

The live run showed Nano inventing links: the api and worker "store state in S3 buckets" (the
Terraform state bucket and the CloudFormation template bucket, both unrelated to the app) and
the worker "uses the same image" (it has its own `checkout-worker` image). Without editing
`agents/`, Phase 3 now:

- marks tooling with `app.graph.roles.role()` (Terraform state and lock stores, the template
  bucket, Groundcrew's own role, Resource Explorer) and tags it in the digest notes as
  `role=tooling (manages the app's infrastructure; the app does not use it)` unless an edge
  connects it to the app;
- states facts explicitly: `own image <repo>:<tag>` per service, and relations with the
  target's type (`pulls_from checkout-worker (ECR image repository)`);
- checks every summary bullet against the graph (`app.graph.summary_check.check_summary`)
  and drops bullets that name unlinked tooling, claim a kind of resource the app does not
  have, say services share an image when they do not, relate a resource with no edges, or
  claim a relationship (calls, reads, uses, deploys, ...) between named resources that no edge
  connects. Names written as a list ("checkout-api and checkout-worker") are not related to
  each other by the claim; Unicode hyphens are normalised first. A workflow is also named by
  its file (`deploy.yml`), and a bullet saying a workflow provisions, creates, or manages
  infrastructure is dropped unless one of its jobs runs an IaC apply (`provisions_infra`).
  Each dropped bullet is a `check_summary` step in the discovery run.

**Proposed tweak to `SYSTEM_PROMPT` in `agents/scout/summary.py` (Ahmed's call):** append
"Only state a relationship that appears in a resource's `relations`. Do not say two
resources share anything (an image, a bucket, a secret) unless both list it. Never describe
what a resource stores or contains; the input has no such facts." This removes the need for
most of the post-check; we would keep the check as a guard.

## Clients

- AWS `app/connectors/aws.py`: `AwsReader(session).call/paginate` only in
  `DISCOVERY_REGIONS`. Medic should add the reads it needs (e.g. `logs:FilterLogEvents`,
  `cloudwatch:GetMetricData`, both already allowed) through `AwsReader` on a session from
  `CredentialsProvider.aws_session`, never a raw client.
- GitHub `app/connectors/github.py`: `GitHubApp.get(installation_id, path)` (GET only).
- Vercel `app/connectors/vercel.py`: `VercelClient(token, team_id)`; values only for plain
  *_URL / *_HOST.
- Azure and Kubernetes: fixture readers only (`TODO(azure-live)`, `TODO(k8s-live)`).

## Discovery contract

- Start: `POST /workspaces/{id}/discover` (all connections) or `POST /connections/{id}/discover`
  (one). Returns a run (`agent = "discovery"`); progress streams on its SSE like any run.
  Job kinds `discover_workspace` / `discover`, `max_attempts = 1`.
- Read: `GET /workspaces/{id}/graph?provider=&type=&include_stale=` and `GET /resources/{id}`
  (config, edges with evidence, recent deploys).
- Coverage: `app.graph.coverage.register_actionable("ecs:service")` from a target adapter makes
  that type `actionable` on the next discovery. Nothing registers yet.

## For Medic: where to look

Start from the resource the incident names, then follow edges by kind: `reads` (data path;
`details.secret_name` and `details.secret_db` name the secret behind it) and `uses` (compare
the secret's `last_changed_at` with the incident start), `logs_to` (log group to query),
`deployed_by` + `defined_in` with `details.commit` (recent runs and the commit of the running
image), `calls` (upstream callers that break too). `config.running_tasks`,
`config.deployments`, and `config.events` on ECS services hold stopped reasons and rollout
state from the last discovery; call AWS again for anything newer.

## Shared files Phase 3 changed

`app/config.py` (Phase 3 settings), `app/db/models.py` and migration
`20261008_3c1d9e4a7b20` (resource account, stale, last_seen_at; edge confidence; connection
last_discovery), `app/main.py` (routers), `app/worker.py` (handlers `discover` and
`discover_workspace`).
