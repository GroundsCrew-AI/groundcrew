# Groundcrew as an MCP server: plan

Decision (2026-10-23): Groundcrew serves MCP so assistants (Claude, Cursor, VS Code) can work
with it. Groundcrew calling other MCP servers (as Medic evidence) can come later.

## What it gives

An engineer asks their assistant "why is checkout-api failing?" or "deploy the web folder of
checkout-demo to the staging server", and the assistant uses Groundcrew's tools: it reads
incidents, Medic's diagnosis, the Map, and deploys, and it can start work. **Anything that
changes infrastructure still waits for a person to approve it in Groundcrew.** The assistant gets
a link to the approval card and never approves itself.

## Shape

- Endpoint `https://api.groundkrew.com/mcp`, streamable HTTP transport, stateless.
- Python MCP SDK (`mcp`) mounted in the existing FastAPI app, calling the same functions as the
  REST routes, so the permission checks stay the same.
- Auth: a workspace **access token** made in Settings ("MCP access"), with a role (viewer or
  approver), an expiry, and per-token revocation. Stored hashed. The audit log records each
  tool call with the token's name. OAuth for MCP can come later.
- Rate limits per token, like the API's.

## Tools (first set)

| Tool | Does | Policy |
|---|---|---|
| `list_incidents`, `get_incident` | open and recent incidents, the diagnosis, evidence, proposed fix | read |
| `list_resources`, `get_resource` | the Map: what runs where, how it connects | read |
| `list_deploys`, `get_deploy` | deploy status, stages, errors, the plan | read |
| `list_runs`, `get_run` | any agent run with its steps | read |
| `list_approvals` | what is waiting for a person | read |
| `start_deploy` | repo, ref, service folder, target, env names (values as secrets) | write: creates the run; the plan waits for approval in the UI |
| `report_incident` | opens an incident for Medic to investigate | write |
| `retry_fix` | asks Medic to try the fix again | write: the fix still needs approval |

Not offered: approving, rejecting, reading secret values, deleting anything.

## Safety

- Tool output is data. Text from incidents, logs, and repositories can contain instructions,
  so it's marked as untrusted in the tool results, and nothing an assistant sends skips the
  approval step.
- Write tools need an approver token; viewer tokens get read tools only.
- Secrets: env values passed to `start_deploy` are handled like the Deploy form's (sealed,
  never echoed).

## Milestones

1. **M1** Tokens (model, Settings UI, audit), the `/mcp` endpoint, read tools, docs page
   "Use Groundcrew from your assistant" with Claude Code / Cursor setup snippets.
2. **M2** Write tools (`start_deploy`, `report_incident`, `retry_fix`) with approval links.
3. **Later** Groundcrew as an MCP *client*: workspaces add read-only MCP servers (Sentry,
   Datadog, their own runbooks) that Medic queries for evidence.

## Checks

Tool tests through the MCP SDK's client; the tenant-isolation test covers `/mcp` tools
(a token from another workspace gets nothing); a live check from Claude Code against a local
stack.
