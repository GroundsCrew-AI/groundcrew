"""GitHub discovery: linked repositories and their workflows, plus IaC facts from a clone.

Per repository: repo metadata, recent commits and workflow runs (REST), then a shallow clone
through the GitHub App (GitHubAppSource) that app.discovery.iac reads for Terraform,
workflows, Dockerfiles, compose files, and env var names. The clone is always deleted.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from app.connectors.github import GitHubApp
from app.connectors.github_source import GitHubAppSource
from app.discovery.iac import read_repo
from app.discovery.types import (
    ConnectorResult,
    DiscoveredResource,
    Progress,
    RepoFacts,
    Workflow,
    no_progress,
)
from app.repos.source import RepoSourceError

logger = logging.getLogger("groundcrew.discovery.github")

PROVIDER = "github"


def repo_external_id(full_name: str) -> str:
    return f"github:repo:{full_name.lower()}"


def workflow_external_id(full_name: str, path: str) -> str:
    return f"github:workflow:{full_name.lower()}:{path}"


class CheckoutSource(Protocol):
    """What discovery needs from a repository checkout (GitHubAppSource in production)."""

    @property
    def commit(self) -> str | None: ...

    def __enter__(self) -> Path: ...
    def __exit__(self, *exc: object) -> None: ...


# (installation_id, full_name, ref) -> a checkout. Tests pass a local fixture repository.
SourceFactory = Callable[[int, str, str | None], Awaitable[CheckoutSource]]


def _read_clone(source: CheckoutSource, full_name: str) -> RepoFacts:
    with source as root:
        facts = read_repo(root, full_name)
    facts.commit = source.commit
    return facts


def workflow_resource(facts: RepoFacts, workflow: Workflow, owner: str) -> DiscoveredResource:
    runs = [r for r in facts.runs if r.get("path") == workflow.path][:10]
    return DiscoveredResource(
        provider=PROVIDER,
        type="github:workflow",
        external_id=workflow_external_id(facts.repo, workflow.path),
        name=f"{workflow.name} ({workflow.path.rsplit('/', 1)[-1]})",
        account=owner,
        repo=facts.repo,
        iac_path=workflow.path,
        config={
            "workflow": workflow.name,
            "path": workflow.path,
            "html_url": f"{facts.html_url}/blob/{facts.default_branch or 'main'}/{workflow.path}"
            if facts.html_url
            else None,
            "triggers": workflow.triggers,
            "branches": workflow.branches,
            "jobs": [
                {
                    "id": j.id,
                    "name": j.name,
                    "needs": j.needs,
                    "role_to_assume": j.role_to_assume,
                    "pushes_images": j.pushes_images,
                    "ecs_deploy": j.ecs_deploy,
                    "ecs_services": j.ecs_services,
                    "vercel": j.vercel,
                    "vercel_env_keys": j.vercel_env_keys,
                }
                for j in workflow.jobs
            ],
            "recent_runs": runs,
            "last_run": runs[0] if runs else None,
        },
    )


def repo_resource(facts: RepoFacts, info: dict[str, Any], owner: str) -> DiscoveredResource:
    return DiscoveredResource(
        provider=PROVIDER,
        type="github:repository",
        external_id=repo_external_id(facts.repo),
        name=facts.repo,
        account=owner,
        repo=facts.repo,
        config={
            "html_url": info.get("html_url"),
            "default_branch": info.get("default_branch"),
            "private": info.get("private"),
            "pushed_at": info.get("pushed_at"),
            "head_commit": facts.commit,
            "recent_commits": facts.commits[:10],
            "terraform_resources": len(facts.terraform),
            "workflows": [w.path for w in facts.workflows],
            "dockerfiles": [d.path for d in facts.dockerfiles],
            "notes": facts.notes,
        },
    )


async def discover_github(
    app: GitHubApp,
    installation_id: int,
    repos: list[str],
    *,
    progress: Progress = no_progress,
    open_source: SourceFactory | None = None,
) -> ConnectorResult:
    async def clone(installation: int, name: str, ref: str | None) -> CheckoutSource:
        return GitHubAppSource(installation, name, ref, app=app)

    opener = open_source or clone
    result = ConnectorResult()
    for full_name in repos:
        owner = full_name.split("/", 1)[0]
        await progress(f"GitHub {full_name}: reading repository, commits, and runs")
        try:
            info, commits, runs = await asyncio.gather(
                app.repo(installation_id, full_name),
                app.recent_commits(installation_id, full_name),
                app.recent_runs(installation_id, full_name),
            )
        except Exception as exc:
            result.notes.append(f"{full_name}: {exc}")
            await progress(f"GitHub {full_name}: skipped ({exc})")
            continue
        branch = info.get("default_branch")
        try:
            source = await opener(installation_id, full_name, branch)
            facts = await asyncio.to_thread(_read_clone, source, full_name)
        except (RepoSourceError, OSError) as exc:
            facts = RepoFacts(repo=full_name)
            facts.notes.append(f"Clone failed, IaC not read: {exc}")
        facts.html_url = info.get("html_url")
        facts.default_branch = branch
        facts.commits = commits
        facts.runs = runs
        result.repos.append(facts)
        result.resources.append(repo_resource(facts, info, owner))
        result.resources += [workflow_resource(facts, w, owner) for w in facts.workflows]
        await progress(
            f"GitHub {full_name}: {len(facts.workflows)} workflows, "
            f"{len(facts.terraform)} Terraform resources, {len(facts.dockerfiles)} Dockerfiles"
        )
    return result
