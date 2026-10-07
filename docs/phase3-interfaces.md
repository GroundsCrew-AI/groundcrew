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
