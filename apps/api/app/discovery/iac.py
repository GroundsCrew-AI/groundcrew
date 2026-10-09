"""Read what a repository says about the system ("IaC facts").

- Terraform (python-hcl2): resource addresses, names resolved from simple expressions
  (`var.x` defaults, `local.x`, `each.key` over literal sets, string templates), and the
  other resources each block references.
- GitHub Actions workflows: triggers, jobs, roles assumed, image pushes, ECS deploys, and
  Vercel steps.
- Dockerfiles (base images, EXPOSE) and compose files (services, ports, env names).
- Env var names the code reads (process.env.X, os.environ["X"], os.getenv("X")).

Only names and structure are kept. Values from .env files are never read (those files are
skipped), and nothing here runs code from the repository.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any

import hcl2
import yaml
from hcl2 import SerializationOptions

from app.discovery.types import (
    ComposeService,
    DockerImage,
    RepoFacts,
    TerraformResource,
    Workflow,
    WorkflowJob,
    WorkflowStep,
)

logger = logging.getLogger("groundcrew.discovery.iac")

SKIP_DIRS = frozenset(
    {".git", ".terraform", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build"}
)
MAX_FILES = 3000
MAX_FILE_BYTES = 300_000
CODE_SUFFIXES = frozenset({".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".go", ".rb"})

# Attributes that name a resource in AWS (what we match real resources on).
NAME_ATTRIBUTES = (
    "name",
    "family",
    "identifier",
    "bucket",
    "function_name",
    "cluster_identifier",
    "table_name",
)

_HCL_OPTIONS = SerializationOptions(
    with_comments=False, with_meta=False, strip_string_quotes=True, explicit_blocks=False
)
_INTERP = re.compile(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}")
_REF = re.compile(r"(?<![\w.])((?:aws|azurerm|google|kubernetes|vercel)_[a-z0-9_]+)\.([a-z0-9_]+)")


def _walk(root: Path) -> list[Path]:
    """Files under root, skipping vendored and build directories, capped."""
    out: list[Path] = []
    stack = [root]
    while stack and len(out) < MAX_FILES:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in SKIP_DIRS:
                    stack.append(entry)
            elif entry.is_file():
                out.append(entry)
    return out


def _read(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(errors="replace")
    except OSError:
        return None


# ------------------------------------------------------------------------- Terraform


class _TfContext:
    def __init__(self) -> None:
        self.vars: dict[str, Any] = {}
        self.locals: dict[str, Any] = {}

    def evaluate(self, value: Any, each_key: str | None = None) -> str | None:
        """A string with every ${...} resolved, or None if any part is not simple."""
        if not isinstance(value, str):
            return str(value) if isinstance(value, int | float) else None
        failed = False

        def sub(match: re.Match[str]) -> str:
            nonlocal failed
            resolved = self._expr(match.group(1).strip(), each_key)
            if resolved is None:
                failed = True
                return ""
            return resolved

        out = _INTERP.sub(sub, value)
        return None if failed else out

    def _expr(self, expr: str, each_key: str | None) -> str | None:
        if expr in ("each.key", "each.value"):
            return each_key
        if expr.startswith("var."):
            value = self.vars.get(expr[4:])
            return self.evaluate(value, each_key) if isinstance(value, str) else None
        if expr.startswith("local."):
            value = self.locals.get(expr[6:])
            return self.evaluate(value, each_key) if isinstance(value, str) else None
        literal = re.fullmatch(r'"([^"$]*)"', expr)
        return literal.group(1) if literal else None

    def keys(self, for_each: Any) -> list[str] | None:
        """Instance keys of a for_each over a literal set or list (or a local holding one)."""
        if isinstance(for_each, list):
            values = [self.evaluate(v) for v in for_each]
            return None if None in values else [v for v in values if v is not None]
        if not isinstance(for_each, str):
            return None
        inner = for_each.strip()
        match = re.fullmatch(r"\$\{(.*)\}", inner, re.DOTALL)
        expr = match.group(1).strip() if match else inner
        if expr.startswith("local."):
            return self.keys(self.locals.get(expr[6:]))
        if expr.startswith("var."):
            return self.keys(self.vars.get(expr[4:]))
        literal = re.fullmatch(r"(?:toset\()?\[(.*)\]\)?", expr, re.DOTALL)
        if literal:
            return re.findall(r'"([^"]*)"', literal.group(1))
        return None


def _hcl_load(text: str) -> dict[str, Any] | None:
    try:
        data: dict[str, Any] = hcl2.loads(text, serialization_options=_HCL_OPTIONS)
    except Exception:
        return None
    return data


def _blocks(data: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    value = data.get(kind) or []
    return value if isinstance(value, list) else [value]


def _line_of(text: str, rtype: str, name: str) -> int | None:
    match = re.search(rf'resource\s+"{re.escape(rtype)}"\s+"{re.escape(name)}"', text)
    return text.count("\n", 0, match.start()) + 1 if match else None


def parse_terraform(root: Path, files: list[Path]) -> list[TerraformResource]:
    tf_files = [f for f in files if f.suffix == ".tf"]
    by_dir: dict[Path, list[Path]] = {}
    for f in tf_files:
        by_dir.setdefault(f.parent, []).append(f)
    out: list[TerraformResource] = []
    for directory, module_files in sorted(by_dir.items()):
        ctx = _TfContext()
        parsed: list[tuple[Path, str, dict[str, Any]]] = []
        for f in module_files:
            text = _read(f)
            data = _hcl_load(text) if text is not None else None
            if data is None:
                continue
            parsed.append((f, text or "", data))
            for block in _blocks(data, "variable"):
                for name, body in block.items():
                    if isinstance(body, dict) and "default" in body:
                        ctx.vars[name] = body["default"]
            for block in _blocks(data, "locals"):
                ctx.locals.update(block)
        tfvars = directory / "terraform.tfvars"
        if tfvars.is_file():
            data = _hcl_load(_read(tfvars) or "")
            if data:
                ctx.vars.update({k: v for k, v in data.items() if not k.startswith("__")})
        for f, text, data in parsed:
            rel = str(f.relative_to(root))
            for block in _blocks(data, "resource"):
                for rtype, named in block.items():
                    if not isinstance(named, dict):
                        continue
                    for name, body in named.items():
                        if isinstance(body, dict):
                            out += _resource_instances(ctx, rel, text, rtype, name, body)
    return out


def _resource_instances(
    ctx: _TfContext, rel: str, text: str, rtype: str, name: str, body: dict[str, Any]
) -> list[TerraformResource]:
    raw = json.dumps(body)
    refs = sorted({f"{t}.{n}" for t, n in _REF.findall(raw) if not (t == rtype and n == name)})
    line = _line_of(text, rtype, name)
    if "for_each" in body:
        keys = ctx.keys(body["for_each"])
        instances: list[tuple[str, str | None]] = (
            [(f'{rtype}.{name}["{k}"]', k) for k in keys] if keys else [(f"{rtype}.{name}", None)]
        )
    elif "count" in body:
        instances = [(f"{rtype}.{name}[0]", None)]
    else:
        instances = [(f"{rtype}.{name}", None)]
    out = []
    for address, key in instances:
        attributes: dict[str, str] = {}
        for attr in NAME_ATTRIBUTES:
            if attr in body:
                value = ctx.evaluate(body[attr], key)
                if value:
                    attributes[attr] = value
        tags = body.get("tags")
        if isinstance(tags, dict) and "Name" in tags:
            value = ctx.evaluate(tags["Name"], key)
            if value:
                attributes["tags.Name"] = value
        out.append(
            TerraformResource(
                address=address,
                type=rtype,
                path=rel,
                line=line,
                attributes=attributes,
                references=refs,
            )
        )
    return out


# --------------------------------------------------------------------- GitHub Actions


def _str_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if v is not None}


# A shell variable filled from a command that finds a task's IP: ip=$(scripts/api-ip.sh),
# IP=$(aws ecs describe-tasks ...), ...
_IP_ASSIGN = re.compile(
    r"\b(\w+)=\$\(([^)]*(?:\bip\b|-ip\b|_ip\b|ip\.sh|describe-tasks|publicIp|"
    r"describe-network-interfaces)[^)]*)\)",
    re.IGNORECASE,
)


def _env_sources(run: str) -> dict[str, str]:
    """{"API_URL": "task_ip"} for KEY="...$ip..." when $ip came from a task-IP command."""
    ip_vars = {m.group(1) for m in _IP_ASSIGN.finditer(run)}
    out: dict[str, str] = {}
    for key, value in re.findall(r"\b([A-Z][A-Z0-9_]*)=(\"[^\"]*\"|\S+)", run):
        if any(re.search(rf"\$\{{?{re.escape(v)}\b", value) for v in ip_vars):
            out[key] = "task_ip"
    return out


def _job_facts(job: WorkflowJob, workflow_env: dict[str, str]) -> WorkflowJob:
    env = {**workflow_env, **job.env}
    for step in job.steps:
        uses = (step.uses or "").lower()
        run = step.run or ""
        step_env = {**env, **step.env}
        if uses.startswith("aws-actions/configure-aws-credentials"):
            role = step.inputs.get("role-to-assume")
            if role:
                job.role_to_assume.append(role)
        if "docker push" in run or (
            uses.startswith("docker/build-push-action") and step.inputs.get("push") == "true"
        ):
            job.pushes_images += sorted(
                v for k, v in step_env.items() if "ECR" in k.upper() and "REPO" in k.upper()
            )
        if "aws ecs update-service" in run or uses.startswith(
            "aws-actions/amazon-ecs-deploy-task-definition"
        ):
            job.ecs_deploy = True
            for var in re.findall(r"--service\s+\"?\$\{?(\w+)", run):
                if var in step_env:
                    job.ecs_services.append(step_env[var])
            if step.inputs.get("service"):
                job.ecs_services.append(step.inputs["service"])
        if "vercel" in uses or "vercel" in run.lower() or "VERCEL_TOKEN" in step_env:
            job.vercel = True
            job.vercel_env_keys += re.findall(r"\b([A-Z][A-Z0-9_]*)=\S+\s+\S*vercel", run)
            job.vercel_env_keys += re.findall(r"vercel\s+env\s+add\s+([A-Z][A-Z0-9_]*)", run)
            # The ECS services whose details feed this Vercel step (e.g. the api's IP).
            job.ecs_services += [v for k, v in step_env.items() if k.upper() == "SERVICE"]
            job.vercel_env_sources.update(_env_sources(run))
    job.role_to_assume = sorted(set(job.role_to_assume))
    job.pushes_images = sorted(set(job.pushes_images))
    job.ecs_services = sorted(set(job.ecs_services))
    job.vercel_env_keys = sorted(set(job.vercel_env_keys))
    return job


def parse_workflow(rel: str, text: str) -> Workflow | None:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    # YAML 1.1 reads the key `on` as True.
    on = data.get("on", data.get(True))
    triggers: list[str] = []
    branches: list[str] = []
    if isinstance(on, str):
        triggers = [on]
    elif isinstance(on, list):
        triggers = [str(t) for t in on]
    elif isinstance(on, dict):
        triggers = [str(t) for t in on]
        push = on.get("push")
        if isinstance(push, dict):
            branches = [str(b) for b in push.get("branches") or []]
    workflow_env = _str_map(data.get("env"))
    jobs: list[WorkflowJob] = []
    for job_id, body in (data.get("jobs") or {}).items():
        if not isinstance(body, dict):
            continue
        steps = [
            WorkflowStep(
                name=s.get("name"),
                uses=s.get("uses"),
                inputs=_str_map(s.get("with")),
                env=_str_map(s.get("env")),
                run=s.get("run"),
            )
            for s in body.get("steps") or []
            if isinstance(s, dict)
        ]
        needs = body.get("needs") or []
        job = WorkflowJob(
            id=str(job_id),
            name=body.get("name"),
            needs=[needs] if isinstance(needs, str) else [str(n) for n in needs],
            env=_str_map(body.get("env")),
            steps=steps,
        )
        jobs.append(_job_facts(job, workflow_env))
    return Workflow(
        path=rel,
        name=str(data.get("name") or Path(rel).stem),
        triggers=triggers,
        branches=branches,
        env=workflow_env,
        jobs=jobs,
    )


# ----------------------------------------------------------------- Docker and compose


def parse_dockerfile(rel: str, text: str) -> DockerImage:
    bases = re.findall(r"(?im)^\s*FROM\s+(?:--\S+\s+)*(\S+)", text)
    ports = [int(p) for p in re.findall(r"(?im)^\s*EXPOSE\s+(\d+)", text)]
    return DockerImage(path=rel, base_images=bases, exposed_ports=ports)


def parse_compose(rel: str, text: str) -> list[ComposeService]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    services = (data or {}).get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return []
    out = []
    for name, body in services.items():
        if not isinstance(body, dict):
            continue
        env = body.get("environment") or {}
        env_names = (
            sorted(env) if isinstance(env, dict) else sorted(str(e).split("=", 1)[0] for e in env)
        )
        build = body.get("build")
        depends = body.get("depends_on") or []
        out.append(
            ComposeService(
                path=rel,
                name=str(name),
                image=body.get("image"),
                build=build if isinstance(build, str) else (build or {}).get("context"),
                ports=[str(p) for p in body.get("ports") or []],
                depends_on=sorted(depends) if isinstance(depends, dict) else list(depends),
                env_names=env_names,
            )
        )
    return out


_ENV_REF = re.compile(
    r"process\.env\.([A-Z][A-Z0-9_]*)"
    r"|process\.env\[['\"]([A-Z][A-Z0-9_]*)['\"]\]"
    r"|os\.environ(?:\.get)?\(?\[?['\"]([A-Z][A-Z0-9_]*)['\"]"
    r"|os\.getenv\(['\"]([A-Z][A-Z0-9_]*)['\"]"
)


def parse_code_env(rel: str, text: str) -> set[str]:
    return {next(g for g in match if g) for match in _ENV_REF.findall(text)}


# ----------------------------------------------------------------------------- entry


def read_repo(root: Path, repo: str) -> RepoFacts:
    """Everything above for one checked-out repository."""
    files = _walk(root)
    facts = RepoFacts(repo=repo)
    if len(files) >= MAX_FILES:
        facts.notes.append(f"Read the first {MAX_FILES} files only.")
    try:
        facts.terraform = parse_terraform(root, files)
    except Exception as exc:  # a parser bug must not stop discovery
        logger.warning("terraform parse failed", extra={"error": type(exc).__name__})
        facts.notes.append("Terraform could not be parsed.")
    env_refs: dict[str, set[str]] = {}
    for f in files:
        rel = str(f.relative_to(root))
        name = f.name
        if name.startswith(".env"):
            continue  # never read env files
        if rel.startswith(".github/workflows/") and f.suffix in (".yml", ".yaml"):
            text = _read(f)
            workflow = parse_workflow(rel, text) if text else None
            if workflow:
                facts.workflows.append(workflow)
        elif name == "Dockerfile" or name.startswith("Dockerfile."):
            text = _read(f)
            if text:
                facts.dockerfiles.append(parse_dockerfile(rel, text))
        elif re.fullmatch(r"(docker-)?compose(\.[\w-]+)?\.ya?ml", name):
            text = _read(f)
            if text:
                facts.compose += parse_compose(rel, text)
        elif f.suffix in CODE_SUFFIXES:
            text = _read(f)
            if text:
                for var in parse_code_env(rel, text):
                    env_refs.setdefault(var, set()).add(rel)
    facts.code_env_refs = {k: sorted(v)[:5] for k, v in sorted(env_refs.items())}
    return facts
