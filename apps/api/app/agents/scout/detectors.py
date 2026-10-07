"""Deterministic detectors: what can be known from files alone, with file:line evidence.

They run before the model and win over it on conflict. Each value carries a strength:
3 = stated explicitly (a script, Procfile, EXPOSE, a listen call), 2 = derived from code
(the app object found in a file), 1 = a convention or default (Next.js serves on 3000).
A stronger finding replaces a weaker one; on a tie the first one stays.
"""

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from app.agents.scout.appspec import EnvVar, Evidence, FieldName
from app.agents.scout.repo_view import RepoView

MANIFESTS = ("package.json", "requirements.txt", "pyproject.toml", "Pipfile", "go.mod")
ENV_TEMPLATES = (".env.example", ".env.sample", ".env.template", ".env.dist", ".env.defaults")

EXPLICIT, DERIVED, DEFAULT = 3, 2, 1


@dataclass
class Finding:
    value: Any
    strength: int
    evidence: Evidence


@dataclass
class Detection:
    values: dict[str, Finding] = field(default_factory=dict)
    env_vars: dict[str, tuple[EnvVar, Evidence]] = field(default_factory=dict)
    has_dockerfile: Evidence | None = None
    subpath: str | None = None
    notes: list[str] = field(default_factory=list)
    # Whether code reads each env var without a default. Code decides `required`; a name only
    # listed in an env template counts as required.
    _required_in_code: dict[str, bool] = field(default_factory=dict)

    def set(
        self, name: FieldName, value: Any, strength: int, file: str, line: int, reason: str
    ) -> None:
        current = self.values.get(name)
        if current is not None and current.strength >= strength:
            return
        evidence = Evidence(field=name, file=file, line=max(1, line), reason=reason)
        self.values[name] = Finding(value, strength, evidence)

    def env(
        self, var: EnvVar, file: str, line: int, reason: str, *, from_code: bool = False
    ) -> None:
        if from_code:
            seen = self._required_in_code.get(var.name, False)
            self._required_in_code[var.name] = seen or var.required
        evidence = Evidence(field="env_vars", file=file, line=max(1, line), reason=reason)
        existing = self.env_vars.get(var.name)
        if existing is None:
            self.env_vars[var.name] = (var, evidence)
            return
        known, first = existing
        merged = known.model_copy(
            update={
                "required": known.required or var.required,
                "secret": known.secret or var.secret,
                "example": known.example or var.example,
            }
        )
        self.env_vars[var.name] = (merged, first)

    def settle_env(self) -> None:
        for name, (var, evidence) in self.env_vars.items():
            required = self._required_in_code.get(name, True)
            self.env_vars[name] = (var.model_copy(update={"required": required}), evidence)

    def value(self, name: str) -> Any:
        found = self.values.get(name)
        return None if found is None else found.value

    @property
    def evidence(self) -> list[Evidence]:
        items = [f.evidence for f in self.values.values()]
        items += [e for _, e in self.env_vars.values()]
        if self.has_dockerfile is not None:
            items.append(self.has_dockerfile)
        return items


# --- helpers -----------------------------------------------------------------------------

_SECRET_WORDS = (
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "PRIVATE",
    "API_KEY",
    "ACCESS_KEY",
    "CREDENTIAL",
    "DSN",
)
_SECRET_NAMES = frozenset({"DATABASE_URL", "REDIS_URL", "MONGODB_URI", "MONGO_URL", "MYSQL_URL"})


def is_secret_name(name: str) -> bool:
    upper = name.upper()
    return (
        upper in _SECRET_NAMES or upper.endswith("_KEY") or any(w in upper for w in _SECRET_WORDS)
    )


def _line_of(view: RepoView, rel: str, needle: str) -> int:
    return view.find(rel, needle) or 1


def _dep_line(view: RepoView, rel: str, package: str) -> int:
    """The line declaring `package` in a manifest: a JSON key, a requirement, or a module path.
    Not just any line containing the word (an app named "express-redis" is not the redis
    client)."""
    lines = view.lines(rel) or []
    for number, text in enumerate(lines, start=1):
        if f'"{package}":' in text or f'"{package}" :' in text:
            return number
    for number, text in enumerate(lines, start=1):
        match = _REQ_NAME.match(text)
        if match and match.group(1).lower().replace("_", "-") == package:
            return number
    for number, text in enumerate(lines, start=1):
        if re.search(rf"(?<![\w-]){re.escape(package)}(?![\w-])", text):
            return number
    return 1


def _module(rel: str) -> str:
    return PurePosixPath(rel).with_suffix("").as_posix().replace("/", ".")


# --- top level ---------------------------------------------------------------------------


def detect(view: RepoView) -> Detection:
    found = Detection()
    _dockerfile(view, found)
    _procfile(view, found)
    if view.exists("package.json"):
        _node(view, found)
    if any(view.exists(m) for m in ("requirements.txt", "pyproject.toml", "Pipfile")):
        _python(view, found)
    if view.exists("go.mod"):
        _go(view, found)
    _env_templates(view, found)
    _source_scan(view, found)
    found.settle_env()
    _database_from_env(found)
    if found.value("framework") is None and found.value("language") is not None:
        lang = found.values["language"].evidence
        found.set("framework", "none", DEFAULT, lang.file, lang.line, "no known web framework")
    return found


def find_subpath(view: RepoView) -> tuple[str | None, list[str]]:
    """For a monorepo with no manifest at the root: the one app directory, or the candidates."""
    if any(view.exists(m) for m in MANIFESTS):
        return None, []
    dirs = sorted(
        {
            str(PurePosixPath(f).parent)
            for f in view.files
            if PurePosixPath(f).name in MANIFESTS and 1 <= len(PurePosixPath(f).parts) - 1 <= 2
        }
    )
    return (dirs[0], dirs) if len(dirs) == 1 else (None, dirs)


# --- Dockerfile and Procfile ---------------------------------------------------------------


def _dockerfile(view: RepoView, found: Detection) -> None:
    if not view.exists("Dockerfile"):
        return
    found.has_dockerfile = Evidence(
        field="has_dockerfile", file="Dockerfile", line=1, reason="Dockerfile at the app root"
    )
    for number, text in enumerate(view.lines("Dockerfile") or [], start=1):
        match = re.match(r"\s*EXPOSE\s+(\d+)", text, re.IGNORECASE)
        if match:
            found.set("port", int(match.group(1)), EXPLICIT, "Dockerfile", number, "EXPOSE")
            break


def _procfile(view: RepoView, found: Detection) -> None:
    for number, text in enumerate(view.lines("Procfile") or [], start=1):
        match = re.match(r"\s*web\s*:\s*(.+)", text)
        if not match:
            continue
        command = match.group(1).strip()
        found.set("start_cmd", command, EXPLICIT, "Procfile", number, "Procfile web process")
        port = re.search(r"(?:--port[ =]|-p |:)(\d{2,5})\b", command)
        if port:
            found.set("port", int(port.group(1)), EXPLICIT, "Procfile", number, "port in command")
        return


# --- Node ----------------------------------------------------------------------------------

_NODE_FRAMEWORKS = (
    ("next", "nextjs"),
    ("@nestjs/core", "nestjs"),
    ("nuxt", "nuxt"),
    ("@sveltejs/kit", "sveltekit"),
    ("@remix-run/node", "remix"),
    ("fastify", "fastify"),
    ("express", "express"),
    ("vite", "vite"),
    ("react", "react"),
)
_DEFAULT_PORTS = {"nextjs": 3000, "nuxt": 3000, "remix": 3000, "nestjs": 3000}
_NODE_DATABASES = (
    (("pg", "postgres", "@neondatabase/serverless", "pg-promise"), "postgres"),
    (("mysql", "mysql2"), "mysql"),
    (("redis", "ioredis"), "redis"),
    (("mongodb", "mongoose"), "other"),
)
_RUN = {"npm": "npm run", "yarn": "yarn", "pnpm": "pnpm run", "bun": "bun run"}
_START = {"npm": "npm start", "yarn": "yarn start", "pnpm": "pnpm start", "bun": "bun run start"}


def _node(view: RepoView, found: Detection) -> None:
    rel = "package.json"
    try:
        data = json.loads(view.text(rel) or "{}")
    except json.JSONDecodeError:
        found.notes.append("package.json is not valid JSON")
        return
    if not isinstance(data, dict):
        return
    deps: dict[str, Any] = {**data.get("devDependencies", {}), **data.get("dependencies", {})}
    scripts: dict[str, str] = data.get("scripts", {}) or {}

    if view.exists("tsconfig.json"):
        found.set("language", "typescript", DERIVED, "tsconfig.json", 1, "tsconfig.json present")
    elif "typescript" in deps:
        line = _line_of(view, rel, '"typescript"')
        found.set("language", "typescript", DERIVED, rel, line, "typescript dependency")
    else:
        found.set("language", "javascript", DERIVED, rel, 1, "package.json, no TypeScript")

    for package, framework in _NODE_FRAMEWORKS:
        if package in deps:
            line = _dep_line(view, rel, package)
            found.set("framework", framework, DERIVED, rel, line, f"{package} dependency")
            if framework in _DEFAULT_PORTS:
                default = _DEFAULT_PORTS[framework]
                found.set("port", default, DEFAULT, rel, line, f"{framework} serves on {default}")
            break

    manager, lockfile = "npm", None
    for name, pm in (
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lockb", "bun"),
        ("bun.lock", "bun"),
        ("package-lock.json", "npm"),
    ):
        if view.exists(name):
            manager, lockfile = pm, name
            break
    declared = str(data.get("packageManager", ""))
    if lockfile:
        found.set("package_manager", manager, EXPLICIT, lockfile, 1, "lockfile")
    elif declared:
        manager = declared.split("@")[0] if declared.split("@")[0] in _RUN else "npm"
        line = _line_of(view, rel, '"packageManager"')
        found.set("package_manager", manager, EXPLICIT, rel, line, "packageManager field")
    else:
        found.set("package_manager", "npm", DEFAULT, rel, 1, "package.json without a lockfile")

    frozen = {
        "npm": "npm ci" if lockfile else "npm install",
        "yarn": "yarn install --frozen-lockfile" if lockfile else "yarn install",
        "pnpm": "pnpm install --frozen-lockfile" if lockfile else "pnpm install",
        "bun": "bun install",
    }[manager]
    found.set("install_cmd", frozen, DERIVED, lockfile or rel, 1, f"{manager} install")

    if "build" in scripts:
        line = _line_of(view, rel, '"build"')
        found.set("build_cmd", f"{_RUN[manager]} build", EXPLICIT, rel, line, "build script")
    if "start" in scripts:
        line = _line_of(view, rel, '"start"')
        found.set(
            "start_cmd", _START[manager], EXPLICIT, rel, line, f"start script: {scripts['start']}"
        )
        in_script = re.search(r"(?:-p|--port)[ =](\d{2,5})|PORT=(\d{2,5})", scripts["start"])
        if in_script:
            value = int(in_script.group(1) or in_script.group(2))
            found.set("port", value, EXPLICIT, rel, line, "port in the start script")
    elif isinstance(data.get("main"), str):
        line = _line_of(view, rel, '"main"')
        found.set("start_cmd", f"node {data['main']}", DERIVED, rel, line, "main entry point")

    engines = data.get("engines") or {}
    if isinstance(engines, dict) and isinstance(engines.get("node"), str):
        version = re.search(r"\d+(?:\.\d+)*", engines["node"])
        if version:
            line = _line_of(view, rel, '"node"')
            found.set("runtime_version", version.group(0), EXPLICIT, rel, line, "engines.node")
    for name in (".nvmrc", ".node-version"):
        text = (view.text(name) or "").strip().lstrip("v")
        if re.fullmatch(r"\d+(?:\.\d+)*", text):
            found.set("runtime_version", text, EXPLICIT, name, 1, name)

    _database_from_deps(view, found, rel, deps, _NODE_DATABASES)


def _database_from_deps(
    view: RepoView,
    found: Detection,
    rel: str,
    deps: dict[str, Any] | set[str],
    table: tuple[tuple[tuple[str, ...], str], ...],
) -> None:
    for packages, database in table:
        for package in packages:
            if package in deps:
                line = _dep_line(view, rel, package)
                found.set("database", database, DERIVED, rel, line, f"{package} dependency")
                return
    found.set("database", "none", DEFAULT, rel, 1, "no database client in the dependencies")


# --- Python --------------------------------------------------------------------------------

_PY_DATABASES = (
    (("psycopg2", "psycopg2-binary", "psycopg", "asyncpg", "pg8000"), "postgres"),
    (("pymysql", "mysqlclient", "aiomysql", "mysql-connector-python"), "mysql"),
    (("redis",), "redis"),
    (("pymongo", "motor"), "other"),
)
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _python_deps(view: RepoView) -> tuple[set[str], str]:
    """Normalized dependency names and the file they came from."""
    names: set[str] = set()
    source = ""
    for text in view.lines("requirements.txt") or []:
        match = _REQ_NAME.match(text)
        if match and not text.strip().startswith(("#", "-")):
            names.add(match.group(1).lower().replace("_", "-"))
            source = "requirements.txt"
    pyproject = view.text("pyproject.toml")
    if pyproject:
        try:
            data = tomllib.loads(pyproject)
        except tomllib.TOMLDecodeError:
            data = {}
        project_deps = data.get("project", {}).get("dependencies", [])
        poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
        for spec in [*project_deps, *poetry_deps]:
            match = _REQ_NAME.match(str(spec))
            if match:
                names.add(match.group(1).lower().replace("_", "-"))
                source = source or "pyproject.toml"
    pipfile = view.text("Pipfile")
    if pipfile:
        try:
            for spec in tomllib.loads(pipfile).get("packages", {}):
                names.add(str(spec).lower().replace("_", "-"))
                source = source or "Pipfile"
        except tomllib.TOMLDecodeError:
            pass
    names.discard("python")
    return names, source or "requirements.txt"


def _python(view: RepoView, found: Detection) -> None:
    deps, rel = _python_deps(view)
    found.set("language", "python", DERIVED, rel, 1, f"{rel} present")

    framework = next((f for f in ("fastapi", "flask", "django") if f in deps), None)
    if framework:
        found.set(
            "framework", framework, DERIVED, rel, _dep_line(view, rel, framework), "dependency"
        )

    pyproject = view.text("pyproject.toml") or ""
    if view.exists("uv.lock"):
        found.set("package_manager", "uv", EXPLICIT, "uv.lock", 1, "lockfile")
        found.set("install_cmd", "uv sync --frozen --no-dev", DERIVED, "uv.lock", 1, "uv")
    elif view.exists("poetry.lock") or "[tool.poetry]" in pyproject:
        where = "poetry.lock" if view.exists("poetry.lock") else "pyproject.toml"
        found.set("package_manager", "poetry", EXPLICIT, where, 1, "poetry")
        found.set(
            "install_cmd", "poetry install --no-root --only main", DERIVED, where, 1, "poetry"
        )
    elif view.exists("Pipfile"):
        found.set("package_manager", "pipenv", EXPLICIT, "Pipfile", 1, "Pipfile")
        found.set(
            "install_cmd", "pipenv install --deploy --system", DERIVED, "Pipfile", 1, "pipenv"
        )
    else:
        found.set("package_manager", "pip", DERIVED, rel, 1, f"{rel} without a lockfile")
        command = (
            "pip install --no-cache-dir -r requirements.txt"
            if rel == "requirements.txt"
            else "pip install --no-cache-dir ."
        )
        found.set("install_cmd", command, DERIVED, rel, 1, "pip")

    _python_runtime(view, found, pyproject)
    _python_entry(view, found, framework, deps)
    _database_from_deps(view, found, rel, deps, _PY_DATABASES)


def _python_runtime(view: RepoView, found: Detection, pyproject: str) -> None:
    text = (view.text(".python-version") or "").strip()
    if re.fullmatch(r"\d+\.\d+(?:\.\d+)?", text):
        found.set("runtime_version", text, EXPLICIT, ".python-version", 1, ".python-version")
        return
    runtime = re.search(r"python-(\d+\.\d+(?:\.\d+)?)", view.text("runtime.txt") or "")
    if runtime:
        found.set("runtime_version", runtime.group(1), EXPLICIT, "runtime.txt", 1, "runtime.txt")
        return
    requires = re.search(r'requires-python\s*=\s*"[^"\d]*(\d+\.\d+)', pyproject)
    if requires:
        line = _line_of(view, "pyproject.toml", "requires-python")
        found.set(
            "runtime_version", requires.group(1), DERIVED, "pyproject.toml", line, "requires-python"
        )


def _python_entry(view: RepoView, found: Detection, framework: str | None, deps: set[str]) -> None:
    """Find the app object and derive a start command (unless a Procfile gave one)."""
    patterns = {
        "fastapi": re.compile(r"^(\w+)\s*=\s*FastAPI\("),
        "flask": re.compile(r"^(\w+)\s*=\s*Flask\("),
    }
    if framework == "django":
        settings = next((f for f in view.files if f.endswith("/wsgi.py")), None)
        if settings:
            project = PurePosixPath(settings).parent.as_posix().replace("/", ".")
            command = (
                f"gunicorn {project}.wsgi --bind 0.0.0.0:8000"
                if "gunicorn" in deps
                else "python manage.py runserver 0.0.0.0:8000"
            )
            found.set("start_cmd", command, DERIVED, settings, 1, "Django WSGI module")
            found.set("port", 8000, DEFAULT, settings, 1, "port in the derived command")
        return
    pattern = patterns.get(framework or "")
    if pattern is None:
        return
    for rel in view.source_files:
        if not rel.endswith(".py"):
            continue
        for number, text in enumerate(view.lines(rel) or [], start=1):
            match = pattern.match(text)
            if not match:
                continue
            target = f"{_module(rel)}:{match.group(1)}"
            if framework == "fastapi":
                command, port = f"uvicorn {target} --host 0.0.0.0 --port 8000", 8000
            elif "gunicorn" in deps:
                command, port = f"gunicorn {target} --bind 0.0.0.0:8000", 8000
            else:
                command, port = f"flask --app {target} run --host 0.0.0.0 --port 5000", 5000
            found.set("start_cmd", command, DERIVED, rel, number, f"{framework} app object")
            found.set("port", port, DEFAULT, rel, number, "port in the derived start command")
            return


# --- Go ------------------------------------------------------------------------------------

_GO_FRAMEWORKS = (
    ("github.com/gin-gonic/gin", "gin"),
    ("github.com/labstack/echo", "echo"),
    ("github.com/gofiber/fiber", "fiber"),
)
_GO_DATABASES = (
    (("github.com/lib/pq", "github.com/jackc/pgx"), "postgres"),
    (("github.com/go-sql-driver/mysql",), "mysql"),
    (("github.com/redis/go-redis", "github.com/go-redis/redis"), "redis"),
)


def _go(view: RepoView, found: Detection) -> None:
    rel = "go.mod"
    text = view.text(rel) or ""
    found.set("language", "go", DERIVED, rel, 1, "go.mod present")
    found.set("package_manager", "go", DERIVED, rel, 1, "go modules")
    found.set("install_cmd", "go mod download", DERIVED, rel, 1, "go modules")
    found.set("build_cmd", "go build -o server .", DERIVED, rel, 1, "go build")
    found.set("start_cmd", "./server", DERIVED, rel, 1, "the built binary")
    version = re.search(r"^go\s+(\d+\.\d+(?:\.\d+)?)", text, re.MULTILINE)
    if version:
        found.set(
            "runtime_version",
            version.group(1),
            EXPLICIT,
            rel,
            _line_of(view, rel, "go "),
            "go directive",
        )
    for module, framework in _GO_FRAMEWORKS:
        if module in text:
            found.set("framework", framework, DERIVED, rel, _line_of(view, rel, module), "require")
            break
    required = {p for packages, _ in _GO_DATABASES for p in packages if p in text}
    _database_from_deps(view, found, rel, required, _GO_DATABASES)


# --- env vars, ports, health ---------------------------------------------------------------


def _env_templates(view: RepoView, found: Detection) -> None:
    for rel in ENV_TEMPLATES:
        for number, text in enumerate(view.lines(rel) or [], start=1):
            match = re.match(r"\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*)$", text)
            if not match:
                continue
            name, raw = match.group(1), match.group(2).strip().strip("'\"")
            secret = is_secret_name(name)
            example: str | None = raw or None
            if secret:
                # Keep only a URL's scheme (it says which database); never a secret value.
                example = raw.split("://")[0] + "://..." if "://" in raw else None
            var = EnvVar(name=name, required=True, secret=secret, example=example)
            found.env(var, rel, number, f"listed in {rel}")


_JS_ENV = re.compile(r"process\.env\.([A-Z][A-Z0-9_]*)(\s*(?:\|\||\?\?)\s*([^\s,;)]+))?")
_PY_ENV_REQUIRED = re.compile(r"os\.environ\[\s*['\"]([A-Z][A-Z0-9_]*)['\"]\s*\]")
_PY_ENV_OPTIONAL = re.compile(
    r"(?:os\.environ\.get|os\.getenv)\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]\s*(,\s*([^)]+))?\)"
)
_GO_ENV = re.compile(r"os\.Getenv\(\s*\"([A-Z][A-Z0-9_]*)\"\s*\)")
_HEALTH = re.compile(
    r"['\"`](/(?:health|healthz|healthcheck|ready|readyz|livez|status|ping))['\"`]"
)
_ROUTE_HINT = re.compile(r"\.(get|route|use|all|handle|HandleFunc|GET)\b|@\w+\.(get|route)", re.I)
_LISTEN_PORT = re.compile(
    r"\.listen\(\s*(\d{2,5})|PORT\s*(?:\|\||\?\?)\s*(\d{2,5})|port\s*=\s*(\d{2,5})|\"(?::|0\.0\.0\.0:)(\d{2,5})\""
)


def _source_scan(view: RepoView, found: Detection) -> None:
    for rel in view.source_files:
        for number, text in enumerate(view.lines(rel) or [], start=1):
            for match in _JS_ENV.finditer(text):
                name, default = match.group(1), match.group(3)
                var = EnvVar(
                    name=name,
                    required=default is None,
                    secret=is_secret_name(name),
                    example=None if is_secret_name(name) else default,
                )
                found.env(var, rel, number, "read in code", from_code=True)
            for match in _PY_ENV_REQUIRED.finditer(text):
                name = match.group(1)
                found.env(
                    EnvVar(name=name, required=True, secret=is_secret_name(name)),
                    rel,
                    number,
                    "read in code (required)",
                    from_code=True,
                )
            for match in _PY_ENV_OPTIONAL.finditer(text):
                name = match.group(1)
                var = EnvVar(
                    name=name, required=match.group(2) is None, secret=is_secret_name(name)
                )
                found.env(var, rel, number, "read in code", from_code=True)
            for match in _GO_ENV.finditer(text):
                name = match.group(1)
                found.env(
                    EnvVar(name=name, required=True, secret=is_secret_name(name)),
                    rel,
                    number,
                    "read in code",
                    from_code=True,
                )
            health = _HEALTH.search(text)
            if health and _ROUTE_HINT.search(text):
                found.set("health_path", health.group(1), EXPLICIT, rel, number, "route defined")
            port = _LISTEN_PORT.search(text)
            if port and ("listen" in text or "PORT" in text or "run(" in text or "Run(" in text):
                value = int(next(g for g in port.groups() if g))
                if 1 <= value <= 65535:
                    found.set("port", value, EXPLICIT, rel, number, "port in code")
    if "PORT" in found.env_vars:
        var, evidence = found.env_vars["PORT"]
        found.env_vars["PORT"] = (var.model_copy(update={"secret": False}), evidence)


def _database_from_env(found: Detection) -> None:
    """Env var names hint at a database when the dependencies did not say."""
    current = found.values.get("database")
    if current is not None and current.value != "none":
        return
    hints = (
        ("DATABASE_URL", "postgres"),
        ("POSTGRES", "postgres"),
        ("PG", "postgres"),
        ("MYSQL", "mysql"),
        ("REDIS", "redis"),
    )
    for name, (var, evidence) in sorted(found.env_vars.items()):
        for prefix, database in hints:
            if not name.startswith(prefix):
                continue
            if prefix == "DATABASE_URL" and var.example:
                scheme = var.example.split("://")[0]
                database = "mysql" if scheme.startswith("mysql") else "postgres"
                if not scheme.startswith(("postgres", "mysql")):
                    continue
            found.set(
                "database", database, DERIVED, evidence.file, evidence.line, f"{name} is used"
            )
            return
