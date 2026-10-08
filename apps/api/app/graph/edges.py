"""Relationship inference: which resource calls, reads, uses, or is deployed by which.

Pure functions over discovered resources and repository facts, so every rule is unit-tested
with fixtures. Each edge has a kind, a confidence (high / medium / low), the rule that made
it, and one line of evidence a person can check. Secret values are never inputs: secrets are
matched by ARN and name only, and Vercel values only exist for plain *_URL / *_HOST vars.

Rules (docs/phase3-interfaces.md has the full table):
 1 sg_db_ingress      service/function -> db        reads        high
 2 task_secret        service -> secret             uses         high
   secret_db          secret -> db                  for          high (Terraform) / medium (name)
 3 image_ecr          service -> ECR repo           pulls_from   high
   image_commit       service -> GitHub repo        built_from   high (SHA seen) / medium
 4 awslogs            service/function -> log group logs_to      high
 5 terraform          resource -> GitHub repo       defined_in   high
 6 oidc_deploy        service -> workflow           deployed_by  high
   vercel_job         vercel project -> workflow    deployed_by  high / medium
 7 env_url            vercel project -> service     calls        high (value matches) / medium
 8 same_repo          vercel project <-> service    same_repo    low
 9 shared_tags        resource <-> resource         related      low
Structural (always high): service runs_in cluster, runs_as role, workflow assumes role and
is defined_in its repo, Vercel deployment_of project, k8s routes_to.
"""

import re
from collections import defaultdict
from collections.abc import Sequence
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from app.discovery.types import DiscoveredResource, RepoFacts, Workflow


class EdgeKind(StrEnum):
    calls = "calls"
    reads = "reads"
    uses = "uses"
    for_ = "for"
    pulls_from = "pulls_from"
    logs_to = "logs_to"
    built_from = "built_from"
    deployed_by = "deployed_by"
    assumes = "assumes"
    defined_in = "defined_in"
    same_repo = "same_repo"
    related = "related"
    runs_in = "runs_in"
    runs_as = "runs_as"
    routes_to = "routes_to"
    deployment_of = "deployment_of"


class Confidence(StrEnum):
    high = "high"
    medium = "medium"
    low = "low"


_RANK = {Confidence.high: 3, Confidence.medium: 2, Confidence.low: 1}


def key(resource: DiscoveredResource) -> str:
    return f"{resource.provider}|{resource.external_id}"


class InferredEdge(BaseModel):
    source: str  # key() of the from resource
    target: str
    kind: EdgeKind
    confidence: Confidence
    rule: str
    evidence: str
    details: dict[str, Any] = Field(default_factory=dict)


class IacLink(BaseModel):
    repo: str
    path: str  # file path, with #L<line> when known
    address: str


class EdgeResult(BaseModel):
    edges: list[InferredEdge] = Field(default_factory=list)
    # Resource key -> where it is defined in IaC (Terraform address, workflow file).
    iac: dict[str, IacLink] = Field(default_factory=dict)


class _Graph:
    def __init__(self, resources: Sequence[DiscoveredResource], repos: Sequence[RepoFacts]):
        self.resources = list(resources)
        self.repos = list(repos)
        self.by_type: dict[str, list[DiscoveredResource]] = defaultdict(list)
        for r in self.resources:
            self.by_type[r.type].append(r)
        self.edges: dict[tuple[str, str, str], InferredEdge] = {}
        self.iac: dict[str, IacLink] = {}

    def of(self, *types: str) -> list[DiscoveredResource]:
        return [r for t in types for r in self.by_type.get(t, [])]

    def add(
        self,
        source: DiscoveredResource,
        target: DiscoveredResource,
        kind: EdgeKind,
        confidence: Confidence,
        rule: str,
        evidence: str,
        **details: Any,
    ) -> None:
        if source is target:
            return
        k = (key(source), key(target), kind.value)
        current = self.edges.get(k)
        if current is not None and _RANK[current.confidence] >= _RANK[confidence]:
            return
        self.edges[k] = InferredEdge(
            source=k[0],
            target=k[1],
            kind=kind,
            confidence=confidence,
            rule=rule,
            evidence=evidence,
            details=details,
        )

    def linked(self, a: DiscoveredResource, b: DiscoveredResource) -> bool:
        ka, kb = key(a), key(b)
        return any({e.source, e.target} == {ka, kb} for e in self.edges.values())

    def repo_of(self, resource: DiscoveredResource) -> str | None:
        return next(
            (
                self._repo_name(e.target)
                for e in self.edges.values()
                if e.source == key(resource)
                and e.kind in (EdgeKind.built_from, EdgeKind.defined_in)
                and self._repo_name(e.target)
            ),
            None,
        )

    def _repo_name(self, target_key: str) -> str | None:
        prefix = "github|github:repo:"
        if target_key.startswith(prefix):
            return target_key[len(prefix) :]
        return None


# ----------------------------------------------------------------------------- helpers


def _containers(service: DiscoveredResource) -> list[dict[str, Any]]:
    return list(service.config.get("containers") or [])


def _sg_ids(resource: DiscoveredResource) -> set[str]:
    if resource.type == "ecs:service":
        return set((resource.config.get("network") or {}).get("security_groups") or [])
    return set(resource.config.get("security_groups") or [])


def _port_covers(rule: dict[str, Any], port: int | None) -> bool:
    if rule.get("protocol") == "-1":
        return True
    low, high = rule.get("from_port"), rule.get("to_port")
    if port is None or low is None or high is None:
        return False
    return bool(int(low) <= port <= int(high))


def _tokens(text: str) -> set[str]:
    stop = {"db", "database", "url", "secret", "the", "and", "for", "prod", "dev", "aws"}
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 2 and t not in stop}


def _host_of(value: str) -> str | None:
    value = value.strip()
    if "://" not in value:
        value = "http://" + value
    try:
        return urlsplit(value).hostname
    except ValueError:
        return None


def _var_name(expr: str) -> str | None:
    """`${{ vars.ECS_API_SERVICE }}` -> ECS_API_SERVICE."""
    match = re.search(r"\b(?:vars|env|secrets)\.([A-Z0-9_]+)", expr)
    return match.group(1) if match else None


def _name_hint(expr: str) -> str | None:
    """A word from a variable name that should appear in the resource's name:
    ECS_API_SERVICE -> api, ECS_WORKER_SERVICE -> worker."""
    var = _var_name(expr)
    if not var:
        return None
    words = [
        w
        for w in var.lower().split("_")
        if w not in {"ecs", "service", "name", "aws", "ecr", "repository", "repo", "arn"}
    ]
    return words[0] if len(words) == 1 else None


# ------------------------------------------------------------------------------- rules


def rule_sg_db(g: _Graph) -> None:
    """1. A security group on the database allows the db port from a group the service uses."""
    sgs = {r.config.get("group_id"): r for r in g.of("ec2:security-group")}
    for db in g.of("rds:db"):
        port = (db.config.get("endpoint") or {}).get("port") or 5432
        for db_sg_id in db.config.get("security_groups") or []:
            db_sg = sgs.get(db_sg_id)
            if db_sg is None:
                continue
            for rule in db_sg.config.get("ingress") or []:
                if not _port_covers(rule, int(port)):
                    continue
                for client in g.of("ecs:service", "lambda:function"):
                    shared = _sg_ids(client) & set(rule.get("source_groups") or [])
                    if not shared:
                        continue
                    source_sg = sgs.get(next(iter(sorted(shared))))
                    source_name = source_sg.name if source_sg else next(iter(sorted(shared)))
                    g.add(
                        client,
                        db,
                        EdgeKind.reads,
                        Confidence.high,
                        "sg_db_ingress",
                        f"Security group {db_sg.name} on {db.name} allows port {port} from "
                        f"{source_name}, which {client.name} uses.",
                        port=port,
                        db_security_group=db_sg_id,
                        source_security_group=sorted(shared),
                    )


def _secret_matches(value_from: str, secret: DiscoveredResource) -> bool:
    arn = secret.external_id
    return value_from == arn or value_from.startswith(arn + ":") or value_from == secret.name


def rule_task_secrets(g: _Graph) -> None:
    """2a. The task definition's secrets block injects a Secrets Manager secret."""
    secrets = g.of("secretsmanager:secret")
    for service in g.of("ecs:service"):
        for container in _containers(service):
            for ref in container.get("secrets") or []:
                value_from = str(ref.get("value_from") or "")
                for secret in secrets:
                    if _secret_matches(value_from, secret):
                        g.add(
                            service,
                            secret,
                            EdgeKind.uses,
                            Confidence.high,
                            "task_secret",
                            f"Task definition {service.config.get('task_family')}:"
                            f"{service.config.get('task_revision')} injects {ref.get('name')} "
                            f"from secret {secret.name} (value not read).",
                            env_name=ref.get("name"),
                        )


def rule_secret_db(g: _Graph, matched: dict[str, DiscoveredResource]) -> None:
    """2b. Which database a secret is for. Terraform wins (the secret version is built from the
    db instance's attributes); otherwise names and tags, medium."""
    dbs = g.of("rds:db")
    for repo in g.repos:
        for tf in repo.terraform:
            if tf.type != "aws_secretsmanager_secret_version":
                continue
            secret_refs = [r for r in tf.references if r.startswith("aws_secretsmanager_secret.")]
            db_refs = [r for r in tf.references if r.startswith("aws_db_instance.")]
            for s_ref in secret_refs:
                for d_ref in db_refs:
                    secret, db = matched.get(s_ref), matched.get(d_ref)
                    if secret is not None and db is not None:
                        g.add(
                            secret,
                            db,
                            EdgeKind.for_,
                            Confidence.high,
                            "secret_db",
                            f"Terraform {tf.address} ({tf.path}) builds this secret from "
                            f"{d_ref}, which is {db.name}. The value was not read.",
                            terraform=tf.address,
                        )
    for secret in g.of("secretsmanager:secret"):
        text = " ".join(
            [secret.name, str(secret.config.get("description") or ""), *secret.tags.values()]
        )
        if not re.search(r"(?i)database|postgres|mysql|rds|\bdb\b|-db\b|_db\b|/db\b", text):
            continue
        for db in dbs:
            if any(e.source == key(secret) and e.target == key(db) for e in g.edges.values()):
                continue
            shared = _tokens(text) & (
                _tokens(db.name) | _tokens(str(db.config.get("db_name") or ""))
            )
            if shared or len(dbs) == 1:
                why = f"shares '{sorted(shared)[0]}' with" if shared else "and the only database is"
                g.add(
                    secret,
                    db,
                    EdgeKind.for_,
                    Confidence.medium,
                    "secret_db",
                    f"Secret {secret.name} names a database {why} {db.name}. "
                    "Matched on names only; the value was not read.",
                )


def _commit_for(tag: str, repos: Sequence[RepoFacts]) -> tuple[RepoFacts, dict[str, Any]] | None:
    for repo in repos:
        for commit in repo.commits:
            if str(commit.get("sha", "")).startswith(tag) or tag.startswith(
                str(commit.get("sha", "x"))
            ):
                return repo, commit
        for run in repo.runs:
            if str(run.get("head_sha", "")).startswith(tag):
                return repo, {
                    "sha": run.get("head_sha"),
                    "message": run.get("name"),
                    "at": run.get("created_at"),
                }
        if repo.commit and repo.commit.startswith(tag):
            return repo, {"sha": repo.commit}
    return None


def rule_images(g: _Graph) -> None:
    """3. Image URI -> ECR repo; a commit-SHA tag -> the GitHub repo and commit."""
    ecr = {str(r.config.get("uri") or ""): r for r in g.of("ecr:repository")}
    github = {r.name.lower(): r for r in g.of("github:repository")}
    for service in g.of("ecs:service", "lambda:function"):
        for container in _containers(service):
            registry, repository = (
                container.get("image_registry"),
                container.get("image_repository"),
            )
            tag = container.get("image_tag")
            repo_res = ecr.get(f"{registry}/{repository}") if registry else None
            if repo_res is not None:
                g.add(
                    service,
                    repo_res,
                    EdgeKind.pulls_from,
                    Confidence.high,
                    "image_ecr",
                    f"Container {container.get('name')} runs {repository}:{tag}, "
                    f"which is in ECR {repo_res.name}.",
                    image=container.get("image"),
                )
            if not tag or not re.fullmatch(r"[0-9a-f]{7,40}", str(tag)):
                continue
            found = _commit_for(str(tag), g.repos)
            if found is None:
                continue
            repo, commit = found
            target = github.get(repo.repo.lower())
            if target is None:
                continue
            url = f"{repo.html_url}/commit/{commit.get('sha')}" if repo.html_url else None
            message = commit.get("message")
            g.add(
                service,
                target,
                EdgeKind.built_from,
                Confidence.high,
                "image_commit",
                f"Image tag {str(tag)[:12]} is commit {str(commit.get('sha'))[:7]} of {repo.repo}"
                + (f" ({message})" if message else "")
                + ".",
                commit=commit.get("sha"),
                commit_url=url,
                commit_message=message,
                committed_at=commit.get("at"),
            )


def rule_logs(g: _Graph) -> None:
    """4. awslogs log driver (or a function's log group) -> the CloudWatch log group."""
    groups: dict[tuple[Any, Any], DiscoveredResource] = {
        (r.name, r.region): r for r in g.of("logs:log-group")
    }
    for service in g.of("ecs:service"):
        for container in _containers(service):
            log = container.get("log") or {}
            group = groups.get((log.get("group"), log.get("region") or service.region))
            if log.get("driver") == "awslogs" and group is not None:
                g.add(
                    service,
                    group,
                    EdgeKind.logs_to,
                    Confidence.high,
                    "awslogs",
                    f"Container {container.get('name')} sends logs to {group.name} "
                    f"(awslogs, prefix {log.get('stream_prefix')}).",
                )
    for fn in g.of("lambda:function"):
        group = groups.get((fn.config.get("log_group"), fn.region))
        if group is not None:
            g.add(
                fn,
                group,
                EdgeKind.logs_to,
                Confidence.high,
                "awslogs",
                f"{fn.name} logs to {group.name}.",
            )


# Terraform resource type -> (our type, attribute in Terraform, how to read ours).
_TF_MATCH: dict[str, tuple[str, str, str]] = {
    "aws_ecs_cluster": ("ecs:cluster", "name", "name"),
    "aws_ecs_service": ("ecs:service", "name", "name"),
    "aws_ecs_task_definition": ("ecs:service", "family", "config.task_family"),
    "aws_ecr_repository": ("ecr:repository", "name", "name"),
    "aws_db_instance": ("rds:db", "identifier", "name"),
    "aws_secretsmanager_secret": ("secretsmanager:secret", "name", "name"),
    "aws_cloudwatch_log_group": ("logs:log-group", "name", "name"),
    "aws_iam_role": ("iam:role", "name", "name"),
    "aws_security_group": ("ec2:security-group", "name", "name"),
    "aws_vpc": ("ec2:vpc", "tags.Name", "name"),
    "aws_lambda_function": ("lambda:function", "function_name", "name"),
    "aws_s3_bucket": ("s3:bucket", "bucket", "name"),
    "azurerm_linux_web_app": ("microsoft.web/sites", "name", "name"),
    "azurerm_container_registry": ("microsoft.containerregistry/registries", "name", "name"),
    "azurerm_postgresql_flexible_server": (
        "microsoft.dbforpostgresql/flexibleservers",
        "name",
        "name",
    ),
}


def rule_terraform(g: _Graph) -> dict[str, DiscoveredResource]:
    """5. Terraform blocks whose resolved name matches a real resource. Returns the Terraform
    address -> resource map that other rules use."""
    matched: dict[str, DiscoveredResource] = {}
    github = {r.name.lower(): r for r in g.of("github:repository")}
    for repo in g.repos:
        repo_res = github.get(repo.repo.lower())
        for tf in repo.terraform:
            spec = _TF_MATCH.get(tf.type)
            if spec is None:
                continue
            our_type, tf_attr, ours = spec
            wanted = tf.attributes.get(tf_attr)
            if not wanted:
                continue
            for resource in g.of(our_type):
                value = (
                    resource.config.get(ours.split(".", 1)[1])
                    if ours.startswith("config.")
                    else resource.name
                )
                if value != wanted:
                    continue
                matched[tf.address] = resource
                matched.setdefault(re.sub(r"\[.*\]$", "", tf.address), resource)
                path = f"{tf.path}#L{tf.line}" if tf.line else tf.path
                if tf.type != "aws_ecs_task_definition" or key(resource) not in g.iac:
                    g.iac[key(resource)] = IacLink(repo=repo.repo, path=path, address=tf.address)
                if repo_res is not None and tf.type != "aws_ecs_task_definition":
                    g.add(
                        resource,
                        repo_res,
                        EdgeKind.defined_in,
                        Confidence.high,
                        "terraform",
                        f"Terraform {tf.address} in {path} has {tf_attr} {wanted}.",
                        address=tf.address,
                        path=path,
                    )
    return matched


def _roles_for_repo(g: _Graph, repo: str) -> list[DiscoveredResource]:
    return [
        r
        for r in g.of("iam:role")
        if repo.lower() in [x.lower() for x in r.config.get("github_repos") or []]
    ]


def _role_allows(role: DiscoveredResource, action: str) -> list[str]:
    """Resources the role's inline policies allow `action` on."""
    service = action.split(":", 1)[0]
    out: list[str] = []
    for statements in (role.config.get("inline_policies") or {}).values():
        for st in statements:
            if st.get("effect") != "Allow":
                continue
            actions = st.get("actions") or []
            if action in actions or f"{service}:*" in actions or "*" in actions:
                out += st.get("resources") or []
    return out


def _workflow_resource(g: _Graph, repo: str, workflow: Workflow) -> DiscoveredResource | None:
    wanted = f"github:workflow:{repo.lower()}:{workflow.path}"
    return next((r for r in g.of("github:workflow") if r.external_id == wanted), None)


def rule_deploys(g: _Graph) -> None:
    """6. Which workflow deploys which service or Vercel project."""
    services = g.of("ecs:service")
    projects = g.of("vercel:project")
    github = {r.name.lower(): r for r in g.of("github:repository")}
    for repo in g.repos:
        repo_res = github.get(repo.repo.lower())
        for workflow in repo.workflows:
            wf = _workflow_resource(g, repo.repo, workflow)
            if wf is None:
                continue
            if repo_res is not None:
                g.add(
                    wf,
                    repo_res,
                    EdgeKind.defined_in,
                    Confidence.high,
                    "workflow_file",
                    f"{workflow.path} in {repo.repo}.",
                )
            for job in workflow.jobs:
                roles: list[DiscoveredResource] = []
                for expr in job.role_to_assume:
                    literal = [r for r in g.of("iam:role") if r.external_id == expr]
                    roles += literal or _roles_for_repo(g, repo.repo)
                for role in {key(r): r for r in roles}.values():
                    subjects = ", ".join(role.config.get("github_oidc_subjects") or [])
                    g.add(
                        wf,
                        role,
                        EdgeKind.assumes,
                        Confidence.high,
                        "oidc_deploy",
                        f"Job {job.id} assumes a role through GitHub OIDC; {role.name} trusts "
                        f"{subjects or repo.repo}.",
                    )
                    if not job.ecs_deploy:
                        continue
                    allowed = _role_allows(role, "ecs:UpdateService")
                    for service in services:
                        if service.external_id in allowed or "*" in allowed:
                            g.add(
                                service,
                                wf,
                                EdgeKind.deployed_by,
                                Confidence.high if "*" not in allowed else Confidence.medium,
                                "oidc_deploy",
                                f"{workflow.path} job {job.id} runs aws ecs update-service with "
                                f"role {role.name}, which only {repo.repo} can assume and which "
                                f"may update {service.name}.",
                                job=job.id,
                                role=role.external_id,
                            )
                if job.ecs_deploy:
                    for expr in job.ecs_services:
                        hint = _name_hint(expr)
                        for service in services:
                            if service.name == expr:
                                confidence = Confidence.high
                                why = f"names {service.name}"
                            elif hint and hint in service.name.lower().split("-"):
                                confidence = Confidence.medium
                                why = f"updates {expr} (name hint '{hint}')"
                            else:
                                continue
                            g.add(
                                service,
                                wf,
                                EdgeKind.deployed_by,
                                confidence,
                                "workflow_ecs",
                                f"{workflow.path} job {job.id} {why}.",
                                job=job.id,
                            )
                if job.vercel:
                    _vercel_deploys(g, repo, workflow, job.id, wf, projects)


def _vercel_deploys(
    g: _Graph,
    repo: RepoFacts,
    workflow: Workflow,
    job_id: str,
    wf: DiscoveredResource,
    projects: list[DiscoveredResource],
) -> None:
    repo_name = repo.repo.split("/", 1)[1].lower()
    text = repo.model_dump_json()
    for project in projects:
        pid = str(project.config.get("project_id") or "")
        if project.repo and project.repo.lower() == repo.repo.lower():
            confidence, why = Confidence.high, f"the project is linked to {repo.repo}"
        elif pid and pid in text:
            confidence, why = Confidence.high, f"the workflow names project {pid}"
        elif project.name.lower() == repo_name:
            confidence, why = (
                Confidence.high,
                f"the project and the repository are both {project.name}",
            )
        elif len(projects) == 1:
            confidence, why = Confidence.medium, "it is the only Vercel project connected"
        else:
            continue
        g.add(
            project,
            wf,
            EdgeKind.deployed_by,
            confidence,
            "vercel_job",
            f"{workflow.path} job {job_id} updates Vercel; {why}.",
            job=job_id,
        )


def _endpoints(g: _Graph) -> list[tuple[str, DiscoveredResource, str]]:
    """(host or IP, resource, what it is) for everything with a reachable address."""
    out: list[tuple[str, DiscoveredResource, str]] = []
    for service in g.of("ecs:service"):
        for task in service.config.get("running_tasks") or []:
            if task.get("public_ip"):
                out.append(
                    (task["public_ip"], service, f"the public IP of a running {service.name} task")
                )
            if task.get("private_ip"):
                out.append(
                    (
                        task["private_ip"],
                        service,
                        f"the private IP of a running {service.name} task",
                    )
                )
    for db in g.of("rds:db"):
        address = (db.config.get("endpoint") or {}).get("address")
        if address:
            out.append((address, db, f"the endpoint of {db.name}"))
    for site in g.of("microsoft.web/sites"):
        host = site.config.get("defaultHostName")
        if host:
            out.append((host, site, f"the host name of {site.name}"))
    for svc in g.of("k8s:service"):
        for ext in svc.config.get("external") or []:
            if ext:
                out.append((ext, svc, f"the load balancer address of {svc.name}"))
    for lb in g.of("elasticloadbalancing:loadbalancer/app"):
        dns = ((lb.config.get("properties") or {}).get("DNSName")) if lb.config else None
        if dns:
            out.append((dns, lb, f"the DNS name of {lb.name}"))
    return out


def _env_readers(repo: RepoFacts, env_name: str) -> list[str]:
    return list(repo.code_env_refs.get(env_name) or [])


def rule_env_urls(g: _Graph) -> None:
    """7. A Vercel project's *_URL env var points at a service: by value (high), or by name
    plus a shared repository (medium)."""
    endpoints = _endpoints(g)
    for project in g.of("vercel:project"):
        values: dict[str, str] = project.config.get("env_values") or {}
        for name, value in values.items():
            host = _host_of(value)
            if not host:
                continue
            for address, target, what in endpoints:
                if address.lower() == host.lower():
                    g.add(
                        project,
                        target,
                        EdgeKind.calls,
                        Confidence.high,
                        "env_url",
                        f"Vercel env {name} (plain) is {value}; {host} is {what}.",
                        env_name=name,
                    )
        _env_by_name(g, project, set(values))


def _project_repo(g: _Graph, project: DiscoveredResource) -> str | None:
    if project.repo:
        return project.repo
    for e in g.edges.values():
        if e.source == key(project) and e.kind == EdgeKind.deployed_by:
            wf = next((r for r in g.of("github:workflow") if key(r) == e.target), None)
            if wf is not None and wf.repo:
                return wf.repo
    return None


def _service_repos(g: _Graph, service: DiscoveredResource) -> set[str]:
    repos: set[str] = set()
    for e in g.edges.values():
        if e.source != key(service):
            continue
        if e.kind in (EdgeKind.built_from, EdgeKind.defined_in) and g._repo_name(e.target):
            repos.add(str(g._repo_name(e.target)))
        if e.kind == EdgeKind.deployed_by:
            wf = next((r for r in g.of("github:workflow") if key(r) == e.target), None)
            if wf is not None and wf.repo:
                repos.add(wf.repo)
    return {r.lower() for r in repos}


def _env_by_name(g: _Graph, project: DiscoveredResource, valued: set[str]) -> None:
    repo_name = _project_repo(g, project)
    if not repo_name:
        return
    repo = next((r for r in g.repos if r.repo.lower() == repo_name.lower()), None)
    env = [
        e
        for e in project.config.get("env") or []
        if str(e.get("key", "")).upper().endswith(("_URL", "_HOST")) and e.get("key") not in valued
    ]
    candidates = [
        s
        for s in g.of("ecs:service")
        if repo_name.lower() in _service_repos(g, s) and any(c.get("ports") for c in _containers(s))
    ]
    for item in env:
        name = str(item["key"])
        hint = name.lower().removesuffix("_url").removesuffix("_host")
        targets = [s for s in candidates if hint and hint in s.name.lower().split("-")]
        targets = targets or (candidates if len(candidates) == 1 else [])
        for target in targets:
            parts = [
                f"Vercel env {name} ({item.get('type')}, value not read)",
                f"{target.name} is deployed from the same repository {repo_name}",
            ]
            if repo is not None:
                readers = _env_readers(repo, name)
                if readers:
                    parts.append(f"{', '.join(readers)} reads {name}")
                for wf in repo.workflows:
                    for job in wf.jobs:
                        if name in job.vercel_env_keys:
                            parts.append(
                                f"{wf.path} job {job.id} sets {name} from "
                                f"{', '.join(job.ecs_services) or 'an ECS service'}"
                            )
            g.add(
                project,
                target,
                EdgeKind.calls,
                Confidence.medium,
                "env_url",
                "; ".join(parts) + ".",
                env_name=name,
            )


def rule_same_repo(g: _Graph) -> None:
    """8. A Vercel project and services built or deployed from the same repository."""
    for project in g.of("vercel:project"):
        repo = _project_repo(g, project)
        if not repo:
            continue
        for service in g.of("ecs:service"):
            if repo.lower() in _service_repos(g, service) and not g.linked(project, service):
                g.add(
                    project,
                    service,
                    EdgeKind.same_repo,
                    Confidence.low,
                    "same_repo",
                    f"Both come from {repo}.",
                )


GENERIC_TAGS = frozenset(
    {
        "name",
        "project",
        "environment",
        "env",
        "owner",
        "managed_by",
        "managedby",
        "terraform",
        "createdby",
        "created_by",
        "cost-center",
        "costcenter",
        "team",
        "stage",
    }
)
MAX_TAG_GROUP = 5


def rule_shared_tags(g: _Graph) -> None:
    """9. Resources sharing a specific tag (small groups only; generic tags ignored)."""
    groups: dict[tuple[str, str], list[DiscoveredResource]] = defaultdict(list)
    for r in g.resources:
        for k, v in r.tags.items():
            if k.lower() not in GENERIC_TAGS and not k.lower().startswith(
                ("aws:", "kubernetes.io")
            ):
                groups[(k, v)].append(r)
    for (k, v), members in sorted(groups.items()):
        if not 2 <= len(members) <= MAX_TAG_GROUP:
            continue
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                if not g.linked(a, b):
                    g.add(
                        a,
                        b,
                        EdgeKind.related,
                        Confidence.low,
                        "shared_tags",
                        f"Both are tagged {k}={v}.",
                    )


_ROLE_FIELDS = {"execution_role_arn": "execution role", "task_role_arn": "task role"}


def rule_structure(g: _Graph) -> None:
    """Facts read straight from configuration (always high)."""
    clusters = {r.external_id: r for r in g.of("ecs:cluster")}
    roles = {r.external_id: r for r in g.of("iam:role")}
    for service in g.of("ecs:service"):
        cluster = clusters.get(str(service.config.get("cluster_arn")))
        if cluster is not None:
            g.add(
                service,
                cluster,
                EdgeKind.runs_in,
                Confidence.high,
                "ecs_cluster",
                f"{service.name} runs in cluster {cluster.name}.",
            )
        for field in _ROLE_FIELDS:
            role = roles.get(str(service.config.get(field)))
            if role is not None:
                g.add(
                    service,
                    role,
                    EdgeKind.runs_as,
                    Confidence.high,
                    "task_role",
                    f"Task definition uses {role.name} as its {_ROLE_FIELDS[field]}.",
                )
    projects = {r.config.get("project_id"): r for r in g.of("vercel:project")}
    for deployment in g.of("vercel:deployment"):
        project = projects.get(deployment.config.get("project_id"))
        if project is not None:
            g.add(
                deployment,
                project,
                EdgeKind.deployment_of,
                Confidence.high,
                "vercel_deployment",
                f"Latest production deployment of {project.name}.",
            )
    _k8s_routes(g)


def _k8s_routes(g: _Graph) -> None:
    workloads = g.of("k8s:deployment", "k8s:statefulset")
    services = g.of("k8s:service")
    for svc in services:
        selector = svc.config.get("selector") or {}
        if not selector:
            continue
        for w in workloads:
            labels = w.config.get("pod_labels") or {}
            same_ns = w.config.get("namespace") == svc.config.get("namespace")
            if same_ns and all(labels.get(k) == v for k, v in selector.items()):
                g.add(
                    svc,
                    w,
                    EdgeKind.routes_to,
                    Confidence.high,
                    "k8s_selector",
                    f"Service selector {selector} matches {w.name}'s pods.",
                )
    for ingress in g.of("k8s:ingress"):
        for backend in ingress.config.get("backends") or []:
            for svc in services:
                if svc.name == backend and svc.config.get("namespace") == ingress.config.get(
                    "namespace"
                ):
                    g.add(
                        ingress,
                        svc,
                        EdgeKind.routes_to,
                        Confidence.high,
                        "k8s_ingress",
                        f"Ingress backend is service {backend}.",
                    )


def infer_edges(resources: Sequence[DiscoveredResource], repos: Sequence[RepoFacts]) -> EdgeResult:
    g = _Graph(resources, repos)
    rule_structure(g)
    matched = rule_terraform(g)
    rule_sg_db(g)
    rule_task_secrets(g)
    rule_secret_db(g, matched)
    rule_images(g)
    rule_logs(g)
    rule_deploys(g)
    rule_env_urls(g)
    _reads_via_secret(g)
    rule_same_repo(g)
    rule_shared_tags(g)
    edges = sorted(g.edges.values(), key=lambda e: (e.kind.value, e.source, e.target))
    return EdgeResult(edges=edges, iac=g.iac)


def _reads_via_secret(g: _Graph) -> None:
    """A service that uses a secret for a database, with no network rule seen, probably reads
    that database (medium)."""
    uses = [e for e in g.edges.values() if e.kind == EdgeKind.uses]
    fors = [e for e in g.edges.values() if e.kind == EdgeKind.for_]
    by_key = {key(r): r for r in g.resources}
    for u in uses:
        for f in fors:
            if f.source != u.target:
                continue
            service, db = by_key.get(u.source), by_key.get(f.target)
            if service is None or db is None:
                continue
            k = (u.source, f.target, EdgeKind.reads.value)
            if k in g.edges:
                continue
            g.add(
                service,
                db,
                EdgeKind.reads,
                Confidence.medium,
                "secret_db",
                f"{service.name} gets {u.details.get('env_name') or 'a secret'} from a secret "
                f"for {db.name}; no network rule between them was seen.",
            )
