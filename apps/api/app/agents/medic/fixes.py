"""Fixes Medic proposes, in order (docs/phase5-interfaces.md, Per scenario).

Emergency actions are chosen by rules, never from model text, and only when production is down
now (health checks failing, a failed rollout, or tasks crashing):

- a secret change started it: `secret_rollback_version` on that secret, then `force_redeploy`
  of every ECS service that `uses` it (tasks read secrets only at start);
- a commit or deploy started it: `ecs_rollback_service` for each crashing service.

A pull request is proposed whenever a commit started it and the repository is connected: the
`propose_fix` route (Ultra, Super in dev) writes full file contents from the commit's patch and
the files as they are now; `check_draft` refuses secrets, workflows, paths outside the repo, and
oversize files, and the approval card shows diffs. Plans and drafts are built, never run, here.
"""

import re
import uuid
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from app.actions.base import ActionPlan, EmergencyActionError, EmergencyActions
from app.agents.medic.events import (
    ChangeMarker,
    FileChange,
    PlanLine,
    ProposedFix,
    PullRequestView,
    Signal,
)
from app.agents.medic.schemas import RootCause
from app.agents.medic.state import FixRecord
from app.connectors.github import GitHubApp
from app.db.models import Connection, Edge, Resource
from app.llm.budget import RunBudget
from app.llm.client import OnCall, TokenFactory
from app.llm.router import Task
from app.tools.github_pr import (
    FixPullRequestError,
    PullRequestDraft,
    check_draft,
    commit_files,
    unified_diff,
)

DOWN_RULES = frozenset({"health_check", "ecs_deployment_failed", "ecs_tasks_stopped"})
CRASH_RULES = frozenset({"ecs_deployment_failed", "ecs_tasks_stopped"})
_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")


@dataclass
class Graph:
    resources: dict[uuid.UUID, Resource]
    edges: list[Edge]
    connections: dict[uuid.UUID, Connection]
    affected: list[uuid.UUID]

    def users_of(self, resource_id: uuid.UUID) -> list[Resource]:
        return [
            self.resources[e.from_id]
            for e in self.edges
            if e.to_id == resource_id and e.kind == "uses" and e.from_id in self.resources
        ]

    def repo_for(self, resource_ids: list[uuid.UUID]) -> Resource | None:
        for rid in resource_ids:
            for e in self.edges:
                if e.from_id == rid and e.kind == "defined_in":
                    target = self.resources.get(e.to_id)
                    if target is not None and target.type == "github:repository":
                        return target
        return next((r for r in self.resources.values() if r.type == "github:repository"), None)


@dataclass
class Proposal:
    records: list[FixRecord] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class FileEdit(BaseModel):
    path: str
    content: str = Field(description="The whole new file, not a diff")


class FixDraft(BaseModel):
    title: str = Field(description="PR title, under 70 characters")
    body: str = Field(description="What broke, what this changes, how to check it")
    rationale: str = Field(description="One sentence for the approval card")
    files: list[FileEdit]


FIX_SYSTEM = """You write the smallest pull request that fixes a production incident's cause.
You get the root cause, the commit that started it (its patch), and the current contents of the
files it touched. Return full new file contents for each file you change or add.

Rules:
- Undo only the harmful part of the commit; keep unrelated changes and the file's style.
- Never edit a database migration that may already have run. To undo a schema change, add a NEW
  migration (the next revision, whose down_revision is the latest existing revision) that
  restores it. For Postgres indexes, create them CONCURRENTLY inside an autocommit block.
- Do not touch secrets, .env files, or CI workflows.
- The body says what broke, what the PR changes, and how to verify it."""


def short(arn: str | None) -> str:
    if not arn:
        return "-"
    if ":secret:" in arn:
        return arn.split(":secret:", 1)[1]
    return arn.rsplit("/", 1)[-1]


def plan_lines(plan: ActionPlan) -> list[PlanLine]:
    if plan.action == "ecs_rollback_service":
        what = f"{plan.params.get('service')} task definition"
        return [PlanLine(what=what, before=short(plan.current), after=short(plan.desired))]
    if plan.action == "secret_rollback_version":
        return [
            PlanLine(
                what=f"{short(plan.target)} AWSCURRENT version",
                before=(plan.current or "-")[:12],
                after=(plan.desired or "-")[:12],
            )
        ]
    return [
        PlanLine(
            what=f"{plan.params.get('service')} tasks",
            before=f"deployment {plan.current or '-'}",
            after="new tasks on the same task definition",
        )
    ]


def _emergency_record(plan: ActionPlan, order: int, title: str, rationale: str) -> FixRecord:
    fix = ProposedFix(
        fix_id=f"fix-{order}",
        kind="emergency",
        order=order,
        title=title,
        rationale=rationale,
        action=plan.action,
        plan=plan,
        plan_lines=plan_lines(plan),
    )
    return FixRecord(fix=fix)


def _secret_for(
    change_ref: str | None, graph: Graph, changes: list[ChangeMarker]
) -> Resource | None:
    secrets = [r for r in graph.resources.values() if r.type == "secretsmanager:secret"]
    if change_ref:
        for r in secrets:
            if r.external_id in change_ref or change_ref.startswith(r.external_id):
                return r
    for c in changes:
        if c.kind == "secret_change" and c.resource_id in graph.resources:
            return graph.resources[c.resource_id]
    return secrets[0] if len(secrets) == 1 else None


def _commit_sha(
    root_cause: RootCause, changes: list[ChangeMarker], first_signal: Signal | None
) -> str | None:
    change = root_cause.triggering_change
    if change is not None and change.ref:
        found = _SHA.search(change.ref)
        if found:
            prefix = found.group(0)
            for c in changes:
                if c.kind == "commit" and c.ref.startswith(prefix):
                    return c.ref
            return prefix if len(prefix) == 40 else None
    if change is not None and change.kind in ("commit", "deploy"):
        commits = [c for c in changes if c.kind == "commit"]
        if first_signal is not None:
            commits = [c for c in commits if c.at <= first_signal.at] or commits
        if commits:
            return commits[-1].ref
    return None


async def emergency(
    root_cause: RootCause,
    signals: list[Signal],
    graph: Graph,
    changes: list[ChangeMarker],
    actions: EmergencyActions | None,
    start_order: int = 1,
) -> Proposal:
    out = Proposal()
    if not any(s.rule in DOWN_RULES for s in signals):
        return out
    change = root_cause.triggering_change
    kinds = {o.kind for o in root_cause.fix_options}
    secret_case = (change is not None and change.kind == "secret_change") or (
        "secret_rollback_version" in kinds
        and (change is None or change.kind in ("config", "other"))
    )
    rollback_case = (change is not None and change.kind in ("commit", "deploy")) or (
        "ecs_rollback_service" in kinds and not secret_case
    )
    if not (secret_case or rollback_case):
        return out
    if actions is None:
        out.notes.append(
            "Production is down, but this connection has no Operate role, so no emergency "
            "action is offered. Fix it through the pull request or by hand."
        )
        return out
    order = start_order
    try:
        if secret_case:
            secret = _secret_for(change.ref if change else None, graph, changes)
            if secret is None or secret.connection_id is None:
                out.notes.append("The changed secret is not in the graph; no rollback offered.")
                return out
            plan = await actions.plan_secret_rollback_version(
                secret.connection_id, secret.external_id
            )
            out.records.append(
                _emergency_record(
                    plan,
                    order,
                    f"Roll back {secret.name} to its previous version",
                    "The secret changed right before the failures; the previous version worked.",
                )
            )
            order += 1
            for svc in graph.users_of(secret.id):
                if svc.type != "ecs:service" or svc.connection_id is None:
                    continue
                cluster = str(svc.config.get("cluster_arn") or "")
                plan = await actions.plan_force_redeploy(svc.connection_id, cluster, svc.name)
                out.records.append(
                    _emergency_record(
                        plan,
                        order,
                        f"Restart {svc.name} so it reads the restored secret",
                        "ECS tasks read secrets only when they start.",
                    )
                )
                order += 1
        else:
            crashing = {s.resource_id for s in signals if s.rule in CRASH_RULES}
            for rid in graph.affected:
                found = graph.resources.get(rid)
                if found is None or found.type != "ecs:service" or rid not in crashing:
                    continue
                conn_id = found.connection_id
                if conn_id is None:
                    continue
                svc = found
                cluster = str(svc.config.get("cluster_arn") or "")
                plan = await actions.plan_ecs_rollback_service(conn_id, cluster, svc.name)
                out.records.append(
                    _emergency_record(
                        plan,
                        order,
                        f"Roll back {svc.name} to {short(plan.desired)}",
                        "Production is down now; the previous revision ran without these failures.",
                    )
                )
                order += 1
    except EmergencyActionError as exc:
        out.notes.append(f"No emergency action: {exc}")
    return out


async def pull_request(
    incident_id: uuid.UUID,
    root_cause: RootCause,
    signals: list[Signal],
    graph: Graph,
    changes: list[ChangeMarker],
    github: GitHubApp | None,
    llm: TokenFactory,
    budget: RunBudget,
    order: int,
    on_call: OnCall | None = None,
) -> Proposal:
    out = Proposal()
    change = root_cause.triggering_change
    if change is None or change.kind not in ("commit", "deploy"):
        return out
    first = min(signals, key=lambda s: s.at) if signals else None
    sha = _commit_sha(root_cause, changes, first)
    repo = graph.repo_for(graph.affected)
    if sha is None or repo is None or repo.connection_id is None or github is None:
        out.notes.append("No fix PR: the commit or its repository is not connected.")
        return out
    conn = graph.connections.get(repo.connection_id)
    if conn is None or "installation_id" not in conn.config:
        out.notes.append("No fix PR: the repository's GitHub connection has no installation.")
        return out
    installation = int(conn.config["installation_id"])
    base = str(repo.config.get("default_branch") or "main")
    files = await commit_files(github, installation, repo.name, sha, base)
    if not files:
        out.notes.append(f"No fix PR: commit {sha[:7]} changed no file Medic may edit.")
        return out
    context = "\n\n".join(
        f"File {f.path} ({f.status} in the commit)\nPatch:\n{f.patch}\n\n"
        f"Current contents on {base}:\n"
        + (f.current if f.current is not None else "(file no longer exists)")
        for f in files
    )
    prompt = [
        {"role": "system", "content": FIX_SYSTEM},
        {
            "role": "user",
            "content": (
                f"Root cause: {root_cause.root_cause}\n\n"
                f"Commit {sha[:7]} ({change.description or ''})\n\n"
                f"{context}"
            ),
        },
    ]
    result = await llm.chat(
        Task.propose_fix, prompt, budget=budget, schema=FixDraft, on_call=on_call
    )
    draft_out: FixDraft = result.parsed
    current = {f.path: f.current for f in files}
    draft = PullRequestDraft(
        installation_id=installation,
        repo=repo.name,
        base=base,
        branch=f"groundcrew/fix-{str(incident_id)[:8]}",
        title=draft_out.title[:120],
        body=f"{draft_out.body}\n\nOpened by Groundcrew Medic for incident {incident_id}.",
        files={f.path.lstrip("/"): f.content for f in draft_out.files},
    )
    try:
        check_draft(draft)
    except FixPullRequestError as exc:
        out.notes.append(f"No fix PR: the draft broke a rule ({exc}).")
        return out
    changed = {path: text for path, text in draft.files.items() if current.get(path) != text}
    if not changed:
        out.notes.append("No fix PR: the proposed change is identical to the files now.")
        return out
    draft = draft.model_copy(update={"files": changed})
    view = PullRequestView(
        repo=draft.repo,
        base=draft.base,
        branch=draft.branch,
        title=draft.title,
        body=draft.body,
        files=[
            FileChange(path=p, diff=unified_diff(p, current.get(p), t))
            for p, t in sorted(changed.items())
        ],
    )
    fix = ProposedFix(
        fix_id=f"fix-{order}",
        kind="pr",
        order=order,
        title=draft.title,
        rationale=draft_out.rationale,
        pull_request=view,
    )
    out.records.append(FixRecord(fix=fix, draft=draft))
    return out
