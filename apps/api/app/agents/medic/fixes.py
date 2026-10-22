"""Fixes Medic proposes, in order (docs/phase5-interfaces.md, Per scenario).

Emergency actions are chosen by rules, never from model text, and only when production is down
now (health checks failing, a failed rollout, or tasks crashing), or degraded (errors up, still
answering) when all of these hold: the diagnosis is high confidence, it names a concrete secret,
config, or deploy change that started it, and either a remembered incident (at or above the
memory threshold) was resolved by applied fixes or the action directly reverts that change.
Such fixes carry `degraded` so the approval card says the service is degraded, not down:

- a secret change started it: `secret_rollback_version` on that secret, then `force_redeploy`
  of every ECS service that `uses` it (tasks read secrets only at start);
- a commit or deploy started it: `ecs_rollback_service` for each crashing ECS service,
  `k8s_rollout_undo` (the previous revision) for each crashing Kubernetes Deployment or
  StatefulSet, and `lambda_rollback_alias` (the previous published version) for each failing
  Lambda function;
- a Kubernetes ConfigMap or Secret change started it (ref "<ns>/configmap/<name>@<version>"):
  no emergency action. Groundcrew never reads their data, so it cannot put the old values
  back, and a restart alone would only reload the broken ones; a note asks the person to
  restore it first;
- an EC2 instance's status check failed: `ec2_reboot_instance`, whatever started it.

A pull request is proposed whenever a commit started it and the repository is connected: the
`propose_fix` route (Ultra, Super in dev) writes full file contents from the commit's patch and
the files as they are now; `check_draft` refuses secrets, workflows, paths outside the repo, and
oversize files, and the approval card shows diffs. Plans and drafts are built, never run, here.
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from app.actions.base import ActionPlan, EmergencyActionError, EmergencyActions
from app.agents.medic.diagnose import matched_change
from app.agents.medic.events import (
    ChangeMarker,
    FileChange,
    MemoryHit,
    PlanLine,
    ProposedFix,
    PullRequestView,
    ResearchResult,
    Signal,
)
from app.agents.medic.schemas import RootCause
from app.agents.medic.state import FixRecord
from app.connectors.github import GitHubApp
from app.db.models import Connection, Edge, Resource
from app.llm.budget import RunBudget
from app.llm.client import OnCall, TokenFactory
from app.llm.router import Task
from app.remote_agents import outdated
from app.tools.github_pr import (
    FixPullRequestError,
    PullRequestDraft,
    check_draft,
    commit_files,
    unified_diff,
)

DOWN_RULES = frozenset(
    {
        "health_check",
        "ecs_deployment_failed",
        "ecs_tasks_stopped",
        "k8s_rollout_failed",
        "k8s_pods_crashing",
        "ec2_status_check",
        "agent_service_down",
    }
)
CRASH_RULES = frozenset(
    {
        "ecs_deployment_failed",
        "ecs_tasks_stopped",
        "k8s_rollout_failed",
        "k8s_pods_crashing",
        "lambda_errors",
    }
)
# Load balancer rules: what they flag is the services behind the balancer.
ALB_RULES = frozenset({"alb_5xx", "alb_unhealthy_targets", "alb_latency"})
# What a rollback acts on, per resource type.
ROLLBACK_TYPES = frozenset({"ecs:service", "k8s:deployment", "k8s:statefulset", "lambda:function"})
# Kubernetes workloads Medic rolls back, by graph type -> the actions' `kind`.
K8S_WORKLOADS = {"k8s:deployment": "deployment", "k8s:statefulset": "statefulset"}
# A Kubernetes ConfigMap or Secret change marker's ref (app.telemetry.k8s.config_ref).
_K8S_CONFIG_REF = re.compile(r"^(?P<ns>[^/]+)/(?P<kind>configmap|secret)/(?P<name>[^@]+)@")


def is_down(signal: Signal) -> bool:
    """Production is down there: a down rule, or an error spike where nearly every operation
    fails (Signal.down; services without health checks)."""
    return signal.rule in DOWN_RULES or signal.down


# A hex word with at least one letter: a deploy's run id (all digits) is not a SHA.
_SHA = re.compile(r"\b(?=[0-9]*[a-f])[0-9a-f]{7,40}\b")


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

    def behind(self, balancers: set[uuid.UUID | None]) -> list[uuid.UUID]:
        """What the given load balancers send traffic to (their `calls` edges)."""
        return [
            e.to_id
            for e in self.edges
            if e.kind == "calls" and e.from_id in balancers and e.to_id in self.resources
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
- The title names the change this PR makes to fix the root cause, e.g. "Recreate
  orders.customer_email index concurrently". Call it a revert or rollback only if it removes the
  commit's changes; never mention alerts, false alarms, or the symptom.
- The body says what broke, what the PR changes, and how to verify it."""


def short(arn: str | None) -> str:
    if not arn:
        return "-"
    if ":secret:" in arn:
        return arn.split(":secret:", 1)[1]
    return arn.rsplit("/", 1)[-1]


def _k8s_revision(ref: str | None) -> str:
    """ "revision 3" from "shop/web revision 3"."""
    return " ".join(ref.rsplit(" ", 2)[-2:]) if ref and " " in ref else "-"


def _k8s_name(plan: ActionPlan) -> str:
    """The Deployment's or StatefulSet's name in a Kubernetes plan's params."""
    return plan.params.get(plan.params.get("kind") or "deployment") or "-"


def k8s_config_change(
    root_cause: RootCause, changes: list[ChangeMarker], marker: ChangeMarker | None
) -> ChangeMarker | None:
    """The Kubernetes ConfigMap or Secret change that started it: the matched marker, else
    (the model named a config or secret change without a matching ref) the newest one
    collected."""
    if marker is not None:
        return marker if _K8S_CONFIG_REF.match(marker.ref) else None
    change = root_cause.triggering_change
    if change is None or change.kind not in ("config", "secret_change"):
        return None
    found = [c for c in changes if _K8S_CONFIG_REF.match(c.ref)]
    return max(found, key=lambda c: c.at) if found else None


def k8s_config_note(marker: ChangeMarker, graph: Graph) -> str:
    """ "ConfigMap shop/web-config changed at <time>; restore it, then Groundcrew can restart
    web." Groundcrew never reads the old values, so it offers no rollback of its own."""
    found = _K8S_CONFIG_REF.match(marker.ref)
    ns, kind, name = (found["ns"], found["kind"], found["name"]) if found else ("-", "", "-")
    label = "ConfigMap" if kind == "configmap" else "Secret"
    users = sorted(
        {
            r.name
            for r in graph.resources.values()
            if r.type in K8S_WORKLOADS and (r.id == marker.resource_id or r.id in graph.affected)
        }
    )
    at = marker.at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"{label} {ns}/{name} changed at {at}; restore it, then Groundcrew can restart "
        f"{', '.join(users) or 'the workloads that use it'}. Groundcrew never reads its values, "
        "so it offers no rollback of its own."
    )


def plan_lines(plan: ActionPlan) -> list[PlanLine]:
    if plan.action == "lambda_rollback_alias":
        return [
            PlanLine(
                what=f"{plan.target} alias version",
                before=(plan.current or "-").rsplit(":", 1)[-1],
                after=(plan.desired or "-").rsplit(":", 1)[-1],
            )
        ]
    if plan.action == "agent_rollback_service":
        return [
            PlanLine(
                what=f"{plan.target} image", before=plan.current or "-", after=plan.desired or "-"
            )
        ]
    if plan.action == "agent_restart_service":
        return [
            PlanLine(
                what=f"{plan.target}",
                before=(plan.current or "-").split(" ", 1)[-1],
                after="restarted",
            )
        ]
    if plan.action == "ec2_reboot_instance":
        return [
            PlanLine(
                what=f"{plan.target}",
                before=(plan.current or "-").split(" ", 1)[-1],
                after="rebooted",
            )
        ]
    if plan.action == "k8s_rollout_undo":
        return [
            PlanLine(
                what=f"{_k8s_name(plan)} pod template",
                before=_k8s_revision(plan.current),
                after=_k8s_revision(plan.desired),
            )
        ]
    if plan.action == "k8s_rollout_restart":
        return [
            PlanLine(
                what=f"{_k8s_name(plan)} pods",
                before=_k8s_revision(plan.current),
                after="new pods on the same template",
            )
        ]
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


def _emergency_record(
    plan: ActionPlan,
    order: int,
    title: str,
    rationale: str,
    depends_on: list[str] | None = None,
    degraded: str | None = None,
) -> FixRecord:
    fix = ProposedFix(
        depends_on=depends_on or [],
        degraded=degraded,
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


def _parse_at(at: str | None) -> datetime | None:
    if not at:
        return None
    try:
        parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _commit_by_prefix(prefix: str, changes: list[ChangeMarker]) -> ChangeMarker | None:
    return next((c for c in changes if c.kind == "commit" and c.ref.startswith(prefix)), None)


def commit_change(
    root_cause: RootCause, changes: list[ChangeMarker], first_signal: Signal | None
) -> tuple[str | None, ChangeMarker | None]:
    """The commit that started it (full SHA when the evidence has it) and the change marker it
    came from. The model may name a deploy by its run id (all digits, never a SHA): the deploy
    marker then gives the commit it shipped and its repository."""
    change = root_cause.triggering_change
    if change is None:
        return None, None
    marker = next((c for c in changes if change.ref and c.ref == change.ref), None)
    if marker is not None:
        sha = marker.ref if marker.kind == "commit" else marker.commit
        if sha:
            found = _commit_by_prefix(sha[:7], changes)
            return (found.ref, found) if found else (sha, marker)
    for text in (change.ref, change.description):
        hit = _SHA.search(text or "")
        if hit:
            prefix = hit.group(0)
            found = _commit_by_prefix(prefix, changes)
            if found is not None:
                return found.ref, found
            deploy = next((c for c in changes if c.commit and c.commit.startswith(prefix)), None)
            if deploy is not None and deploy.commit:
                return deploy.commit, deploy
            # GitHub resolves a short SHA, and the repository may still be found.
            return prefix, None
    if change.kind in ("commit", "deploy"):
        before = _parse_at(change.at) or (first_signal.at if first_signal else None)
        commits = [c for c in changes if c.kind == "commit"]
        if before is not None:
            commits = [c for c in commits if c.at <= before] or commits
        if commits:
            return commits[-1].ref, commits[-1]
    return None, None


@dataclass
class RepoTarget:
    installation_id: int
    name: str
    base: str


def _installation(conn: Connection | None) -> int | None:
    if conn is None or conn.kind != "github" or "installation_id" not in conn.config:
        return None
    return int(conn.config["installation_id"])


async def resolve_repo(
    graph: Graph, marker: ChangeMarker | None, github: GitHubApp
) -> RepoTarget | str:
    """The repository to open the fix PR in, and the installation to open it through, or why
    there is none. The repo named by the GitHub evidence (the commit or deploy) wins over the
    graph; it is matched to the workspace's GitHub connections by name, and when no connection
    lists it, each installation's repository list is read again (repos added on GitHub since)."""
    graph_repo = graph.repo_for(graph.affected)
    name = (marker.repo if marker else None) or (graph_repo.name if graph_repo else None)
    if not name:
        return "No fix PR: no GitHub repository in the evidence or the graph."
    key = name.lower()
    for r in graph.resources.values():
        if r.type != "github:repository" or r.name.lower() != key or r.connection_id is None:
            continue
        inst = _installation(graph.connections.get(r.connection_id))
        if inst is not None:
            return RepoTarget(inst, r.name, str(r.config.get("default_branch") or "main"))
    github_conns = [c for c in graph.connections.values() if _installation(c) is not None]
    for conn in github_conns:
        if key in {str(n).lower() for n in conn.config.get("repos") or []}:
            info = await github.repo(int(conn.config["installation_id"]), name)
            return RepoTarget(
                int(conn.config["installation_id"]),
                name,
                str(info.get("default_branch") or "main"),
            )
    for conn in github_conns:
        inst = int(conn.config["installation_id"])
        for repo in await github.installation_repos(inst):
            if str(repo["full_name"]).lower() == key:
                return RepoTarget(
                    inst, repo["full_name"], str(repo.get("default_branch") or "main")
                )
    return f"No fix PR: {name} is not in any GitHub connection of this workspace."


# A degraded service gets an emergency fix only for a change it can name and revert.
DEGRADED_CHANGES = frozenset({"secret_change", "config", "deploy"})


def _degraded_gate(root_cause: RootCause) -> str | None:
    """Why a degraded (not down) service gets no emergency fix, or None when it may."""
    change = root_cause.triggering_change
    if root_cause.confidence != "high":
        return f"the diagnosis is {root_cause.confidence} confidence, not high"
    if change is None or change.kind not in DEGRADED_CHANGES:
        return "no secret, config, or deploy change was found to have started it"
    return None


def _memory_fixed(memory: MemoryHit | None) -> bool:
    """A remembered incident at or above the threshold whose applied fixes resolved it."""
    return memory is not None and bool(memory.fix_summary)


def _changed_version(
    change_ref: str | None, secret: Resource, changes: list[ChangeMarker]
) -> str | None:
    """The secret version the triggering change set: from its ref ("<arn>:<version>" or the
    bare version), else from the latest change marker on that secret."""
    if change_ref:
        last = change_ref.rsplit(":", 1)[-1]
        if last and last != secret.external_id.rsplit(":", 1)[-1] and "/" not in last:
            return last
    marked = sorted(
        (c for c in changes if c.kind == "secret_change" and c.resource_id == secret.id),
        key=lambda c: c.at,
    )
    return marked[-1].ref.rsplit(":", 1)[-1] if marked else None


def _deployed(change_ref: str | None, changes: list[ChangeMarker]) -> list[ChangeMarker]:
    """The deploy markers the triggering deploy names (by task definition, deployment, or
    commit)."""
    if not change_ref:
        return []
    return [
        c
        for c in changes
        if c.kind == "deploy"
        and (c.ref == change_ref or change_ref in c.ref or (c.commit or "") == change_ref)
    ]


async def emergency(
    root_cause: RootCause,
    signals: list[Signal],
    graph: Graph,
    changes: list[ChangeMarker],
    actions: EmergencyActions | None,
    start_order: int = 1,
    memory: MemoryHit | None = None,
) -> Proposal:
    """Emergency fixes for what started it (a secret, deploy, or commit), then a reboot for
    each EC2 instance whose status check fails."""
    out = await _change_fixes(root_cause, signals, graph, changes, actions, start_order, memory)
    reboots = await _reboots(root_cause, signals, graph, actions, start_order + len(out.records))
    agents = await _agent_fixes(
        root_cause,
        signals,
        graph,
        changes,
        actions,
        start_order + len(out.records) + len(reboots.records),
    )
    for found in (reboots, agents):
        out.records += found.records
        out.notes += found.notes
    return out


async def _reboots(
    root_cause: RootCause,
    signals: list[Signal],
    graph: Graph,
    actions: EmergencyActions | None,
    order: int,
) -> Proposal:
    out = Proposal()
    impaired = list(dict.fromkeys(s.resource_id for s in signals if s.rule == "ec2_status_check"))
    if root_cause.false_alarm or not impaired:
        return out
    if actions is None:
        out.notes.append(
            "An instance's status check fails, but this connection has no Operate role, so no "
            "reboot is offered."
        )
        return out
    for rid in impaired:
        found = graph.resources.get(rid) if rid else None
        if found is None or found.type != "ec2:instance" or found.connection_id is None:
            continue
        try:
            plan = await actions.plan_ec2_reboot_instance(found.connection_id, found.external_id)
        except EmergencyActionError as exc:
            out.notes.append(f"No reboot of {found.name}: {exc}")
            continue
        out.records.append(
            _emergency_record(
                plan,
                order,
                f"Reboot {found.name}",
                "Its status check fails; a reboot restarts the operating system, which clears "
                "most instance check failures.",
            )
        )
        order += 1
    return out


# Agents from this version report images, so a container can be rolled back.
ROLLBACK_SINCE = "0.3.0"
UPGRADE_COMMAND = "curl -fsSL https://groundkrew.com/agent/install.sh | sudo sh"
# Rules that mean a service on an agent's host is down: a restart is the emergency fix.
AGENT_DOWN_RULES = frozenset({"agent_service_down", "health_check"})


async def _agent_fixes(
    root_cause: RootCause,
    signals: list[Signal],
    graph: Graph,
    changes: list[ChangeMarker],
    actions: EmergencyActions | None,
    order: int,
) -> Proposal:
    """For each service on a Groundcrew agent's host that is down or fails its health checks:
    a rollback to the image it ran before when its image changed in the incident window (a bad
    deploy), else a restart. The agent acts only if its configuration allows it; a refused
    rollback falls back to a restart."""
    out = Proposal()
    down = list(
        dict.fromkeys(
            s.resource_id
            for s in signals
            if s.rule in AGENT_DOWN_RULES
            and s.resource_id in graph.resources
            and graph.resources[s.resource_id].type == "agent:service"
        )
    )
    if root_cause.false_alarm or not down:
        return out
    if actions is None:
        out.notes.append("A service on an agent's host is down, but no emergency actions run here.")
        return out
    for rid in down:
        found = graph.resources[rid] if rid else None
        if found is None or found.connection_id is None:
            continue
        host = found.account or "its host"
        conn = graph.connections.get(found.connection_id)
        version = str((conn.config or {}).get("agent_version") or "") if conn else ""
        if found.config.get("runtime") in ("docker", "compose") and outdated(
            version, ROLLBACK_SINCE
        ):
            out.notes.append(
                f"The agent on {host} is version {version}; from {ROLLBACK_SINCE} it remembers the "
                "images a container ran, so Medic can roll it back after a bad deploy. Upgrade it: "
                f"{UPGRADE_COMMAND}"
            )
        deployed = [c for c in changes if c.kind == "deploy" and c.resource_id == rid]
        if deployed:
            try:
                plan = await actions.plan_agent_rollback_service(found.connection_id, found.name)
            except EmergencyActionError as exc:
                out.notes.append(f"No rollback of {found.name}: {exc}")
            else:
                out.records.append(
                    _emergency_record(
                        plan,
                        order,
                        f"Roll back {found.name} on {host} to {plan.desired}",
                        "Its image changed right before it failed; the agent recreates it from "
                        "the image it ran before, with the same settings and volumes, and keeps "
                        "the current container if that one does not come up healthy.",
                    )
                )
                order += 1
                continue
        try:
            plan = await actions.plan_agent_restart_service(found.connection_id, found.name)
        except EmergencyActionError as exc:
            out.notes.append(f"No restart of {found.name}: {exc}")
            continue
        out.records.append(
            _emergency_record(
                plan,
                order,
                f"Restart {found.name} on {host}",
                "It stopped or fails its health checks; the agent on its host restarts it, and "
                "Medic checks it runs and passes them again.",
            )
        )
        order += 1
    return out


async def _change_fixes(
    root_cause: RootCause,
    signals: list[Signal],
    graph: Graph,
    changes: list[ChangeMarker],
    actions: EmergencyActions | None,
    start_order: int = 1,
    memory: MemoryHit | None = None,
) -> Proposal:
    """Emergency fixes for a down service, or for a degraded one under the rules above.
    `memory` is the remembered incident Medic reused (at or above the threshold), if any."""
    out = Proposal()
    if root_cause.false_alarm or not signals:
        return out
    # A Kubernetes ConfigMap or Secret change: no rollback without its old values, which
    # Groundcrew never reads; only a note on how to recover.
    k8s_config = k8s_config_change(root_cause, changes, matched_change(root_cause, changes))
    if k8s_config is not None:
        out.notes.append(k8s_config_note(k8s_config, graph))
        return out
    down = any(is_down(s) for s in signals)
    change = root_cause.triggering_change
    if not down:
        refused = _degraded_gate(root_cause)
        if refused is not None:
            if change is not None and change.kind in DEGRADED_CHANGES:
                out.notes.append(
                    f"The service is degraded, not down, and {refused}, so no emergency "
                    "action is offered."
                )
            return out
    # Which emergency action fits is decided from the evidence: the change the post-check
    # matched to a collected marker, else the kinds of change actually collected. The model's
    # fix_options only break a tie; model text alone never picks an action.
    marker = matched_change(root_cause, changes)
    kinds = {o.kind for o in root_cause.fix_options}
    # Kubernetes ConfigMap / Secret markers never pick the AWS secret rollback.
    seen = {c.kind for c in changes if not _K8S_CONFIG_REF.match(c.ref)}
    if marker is not None:
        secret_case = marker.kind == "secret_change"
        rollback_case = marker.kind in ("commit", "deploy")
    else:
        secret_case = "secret_change" in seen and "secret_rollback_version" in kinds
        rollback_case = (
            not secret_case
            and bool(seen & {"commit", "deploy"})
            and bool({"ecs_rollback_service", "k8s_rollout_undo", "lambda_rollback_alias"} & kinds)
        )
    if not (secret_case or rollback_case):
        return out
    if actions is None:
        out.notes.append(
            ("Production is down" if down else "The service is degraded")
            + ", but this connection has no Operate role, so no emergency "
            "action is offered. Fix it through the pull request or by hand."
        )
        return out
    remembered = _memory_fixed(memory)

    def degraded(revert: bool) -> str | None:
        """The approval card's reason for a degraded service's fix (None when down)."""
        if down:
            return None
        if not revert and memory is not None:
            return (
                f"The same fix resolved {memory.title} ({round(memory.similarity * 100)}% similar)."
            )
        return "It reverts the change that started the errors."

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
            # A direct revert: AWSCURRENT is still the version the triggering change set.
            revert = (
                change is not None
                and change.kind == "secret_change"
                and plan.current is not None
                and plan.current == _changed_version(change.ref, secret, changes)
            )
            if not down and not (revert or remembered):
                out.notes.append(
                    f"The service is degraded, not down; rolling back {secret.name} would not "
                    "revert the triggering change and no remembered incident was fixed this "
                    "way, so no emergency action is offered."
                )
                return out
            reason = degraded(revert)
            rollback = _emergency_record(
                plan,
                order,
                f"Roll back {secret.name} to its previous version",
                "The secret changed right before the failures; the previous version worked.",
                degraded=reason,
            )
            out.records.append(rollback)
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
                        "ECS tasks read secrets only when they start. Runs after the rollback.",
                        depends_on=[rollback.fix.fix_id],
                        degraded=reason,
                    )
                )
                order += 1
        else:
            # A failing load balancer: the services behind it that were just deployed.
            behind = graph.behind({s.resource_id for s in signals if s.rule in ALB_RULES})
            deployed_now = {c.resource_id for c in changes if c.kind == "deploy"}
            if down:
                targets = {s.resource_id for s in signals if s.rule in CRASH_RULES or s.down}
                targets |= {r for r in behind if r in deployed_now}
                deployed: dict[uuid.UUID, str] = {}
            else:
                # Degraded: only the services the triggering deploy changed, and only for a
                # deploy (a commit alone is not a change Medic can revert in place).
                markers = (
                    _deployed(change.ref, changes)
                    if change is not None and change.kind == "deploy"
                    else []
                )
                deployed = {m.resource_id: m.ref for m in markers if m.resource_id}
                signaled = {s.resource_id for s in signals} | set(behind)
                targets = {r for r in deployed if r in signaled or r in graph.affected}
            order_by = (
                list(dict.fromkeys([*graph.affected, *behind]))
                if down
                else sorted(deployed, key=str)
            )
            for rid in order_by:
                found = graph.resources.get(rid)
                if found is None or found.type not in ROLLBACK_TYPES or rid not in targets:
                    continue
                conn_id = found.connection_id
                if conn_id is None:
                    continue
                svc = found
                if svc.type in K8S_WORKLOADS:
                    namespace = str(svc.config.get("namespace") or svc.region or "default")
                    plan = await actions.plan_k8s_rollout_undo(
                        conn_id, namespace, svc.name, K8S_WORKLOADS[svc.type]
                    )
                    to = _k8s_revision(plan.desired)
                elif svc.type == "lambda:function":
                    plan = await actions.plan_lambda_rollback_alias(conn_id, svc.external_id)
                    to = f"version {(plan.desired or '-').rsplit(':', 1)[-1]}"
                else:
                    cluster = str(svc.config.get("cluster_arn") or "")
                    plan = await actions.plan_ecs_rollback_service(conn_id, cluster, svc.name)
                    to = short(plan.desired)
                revert = plan.current is not None and plan.current == deployed.get(rid)
                if not down and not (revert or remembered):
                    out.notes.append(
                        f"{svc.name} is degraded, not down; rolling it back would not revert "
                        "the triggering deploy and no remembered incident was fixed this way."
                    )
                    continue
                out.records.append(
                    _emergency_record(
                        plan,
                        order,
                        f"Roll back {svc.name} to {to}",
                        "Production is down now; the previous revision ran without these failures."
                        if down
                        else "The errors started with this deploy; the previous revision ran "
                        "without them.",
                        degraded=degraded(revert),
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
    research: list[ResearchResult] | None = None,
) -> Proposal:
    out = Proposal()
    change = root_cause.triggering_change
    if root_cause.false_alarm or change is None or change.kind not in ("commit", "deploy"):
        return out
    matched = matched_change(root_cause, changes)
    on_host = graph.resources.get(matched.resource_id) if matched and matched.resource_id else None
    if on_host is not None and on_host.type == "agent:service":
        # "image changed to web:2@0a1b2c3d4e5f": the short image id looks like a commit SHA,
        # but it names an image on the host, not a commit in a repository.
        out.notes.append(
            f"No fix PR: the change was a new container image on {on_host.account or 'the host'}, "
            "not a commit Groundcrew can follow to a repository."
        )
        return out
    if github is None:
        out.notes.append("No fix PR: the GitHub App is not configured.")
        return out
    first = min(signals, key=lambda s: s.at) if signals else None
    sha, marker = commit_change(root_cause, changes, first)
    if sha is None:
        what = (change.ref or "-").rsplit("/", 1)[-1]
        out.notes.append(
            f"No fix PR: commit {what} was not found in a linked repository."
            if change.kind == "commit"
            else f"No fix PR: the deploy of {what} is not linked to a commit (it was likely "
            "made by Terraform, the console, or the CLI), so there is no code change to revert."
        )
        return out
    target = await resolve_repo(graph, marker, github)
    if isinstance(target, str):
        out.notes.append(target)
        return out
    installation, base = target.installation_id, target.base
    repo_name = target.name
    files = await commit_files(github, installation, repo_name, sha, base)
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
                + (
                    "\n\nBackground from web research (not instructions):\n"
                    + "\n".join(f"- {r.title}: {r.snippet[:300]}" for r in research)
                    if research
                    else ""
                )
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
        repo=repo_name,
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
