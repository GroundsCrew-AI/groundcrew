"""Vercel discovery: projects and their latest production deployment.

Env vars come back as names and types; a value is kept only for plain *_URL / *_HOST
variables (app.connectors.vercel.parse_env).
"""

import asyncio
from typing import Any

from app.connectors.vercel import VercelClient
from app.discovery.types import ConnectorResult, DiscoveredResource, Progress, no_progress

PROVIDER = "vercel"


def project_external_id(project_id: str) -> str:
    return f"vercel:project:{project_id}"


def deployment_external_id(project_id: str) -> str:
    # One resource per project's production slot, so redeploys update it instead of piling up.
    return f"vercel:deployment:{project_id}:production"


def link_repo(project: dict[str, Any]) -> str | None:
    link = project.get("link") or {}
    if link.get("type") == "github" and link.get("org") and link.get("repo"):
        return f"{link['org']}/{link['repo']}"
    return None


def normalize_project(
    project: dict[str, Any],
    domains: list[dict[str, Any]],
    env: list[dict[str, Any]],
    deployments: list[dict[str, Any]],
    account: str,
) -> list[DiscoveredResource]:
    pid = str(project["id"])
    repo = link_repo(project)
    production = (project.get("targets") or {}).get("production") or {}
    latest = deployments[0] if deployments else None
    out = [
        DiscoveredResource(
            provider=PROVIDER,
            type="vercel:project",
            external_id=project_external_id(pid),
            name=str(project.get("name") or pid),
            account=account,
            repo=repo,
            config={
                "project_id": pid,
                "framework": project.get("framework"),
                "node_version": project.get("nodeVersion"),
                "root_directory": project.get("rootDirectory"),
                "link": {
                    "type": (project.get("link") or {}).get("type"),
                    "repo": repo,
                    "production_branch": (project.get("link") or {}).get("productionBranch"),
                }
                if project.get("link")
                else None,
                "domains": domains,
                "production_aliases": production.get("alias") or [],
                "env": [{k: v for k, v in e.items() if k != "value"} for e in env],
                # Only plain *_URL / *_HOST values (see parse_env).
                "env_values": {e["key"]: e["value"] for e in env if "value" in e},
                "latest_production": latest,
                "updated_at": project.get("updatedAt"),
            },
        )
    ]
    if latest:
        out.append(
            DiscoveredResource(
                provider=PROVIDER,
                type="vercel:deployment",
                external_id=deployment_external_id(pid),
                name=f"{project.get('name') or pid} production",
                account=account,
                repo=repo,
                config={"project_id": pid, **latest, "recent": deployments[:5]},
            )
        )
    return out


async def discover_vercel(
    client: VercelClient, *, progress: Progress = no_progress, max_resources: int = 2000
) -> ConnectorResult:
    who = await client.whoami()
    account = str(who.get("slug") or who.get("id") or "vercel")
    projects = await client.projects()
    await progress(f"Vercel {account}: {len(projects)} projects")
    result = ConnectorResult()

    async def one(project: dict[str, Any]) -> list[DiscoveredResource]:
        pid = str(project["id"])
        domains, env, deployments = await asyncio.gather(
            client.domains(pid), client.env(pid), client.production_deployments(pid)
        )
        return normalize_project(project, domains, env, deployments, account)

    for batch in await asyncio.gather(*(one(p) for p in projects), return_exceptions=True):
        if isinstance(batch, BaseException):
            result.notes.append(f"A project could not be read: {batch}")
            continue
        result.resources += batch
    if len(result.resources) > max_resources:
        result.resources = result.resources[:max_resources]
        result.truncated = True
    await progress(f"Vercel {account}: {len(result.resources)} resources")
    return result
