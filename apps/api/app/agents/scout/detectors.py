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

MANIFESTS = (
    "package.json",
    "requirements.txt",
    "pyproject.toml",
    "Pipfile",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
    "Cargo.toml",
    "composer.json",
)
# .NET projects and solutions are named after the app (Api.csproj, Shop.sln).
MANIFEST_SUFFIXES = (".csproj", ".fsproj", ".sln")
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


def is_manifest(name: str) -> bool:
    return name in MANIFESTS or name.endswith(MANIFEST_SUFFIXES)


def detect(view: RepoView) -> Detection:
    found = Detection()
    _dockerfile(view, found)
    _procfile(view, found)
    # JVM, Ruby, Rust, PHP, and .NET apps often carry a package.json for front-end assets
    # (Rails, Laravel): the server language decides, and the Node rules are skipped.
    backend = False
    if view.exists("pom.xml"):
        _maven(view, found)
        backend = True
    elif gradle := _gradle_file(view):
        _gradle(view, found, gradle)
        backend = True
    if view.exists("Gemfile"):
        _ruby(view, found)
        backend = True
    if view.exists("Cargo.toml"):
        _rust(view, found)
        backend = True
    if view.exists("composer.json") or _plain_php_entry(view):
        _php(view, found)
        backend = True
    if project := _dotnet_project(view):
        _dotnet(view, found, project)
        backend = True
    if view.exists("package.json") and backend:
        found.notes.append("package.json is taken as front-end assets of the server app")
    elif view.exists("package.json"):
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
    if any(is_manifest(f) for f in view.files if "/" not in f):
        return None, []
    dirs = sorted(
        {
            str(PurePosixPath(f).parent)
            for f in view.files
            if is_manifest(PurePosixPath(f).name) and 1 <= len(PurePosixPath(f).parts) - 1 <= 2
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
                # "sqlx:postgres" is a crate feature: the line is the crate's.
                line = _dep_line(view, rel, package.split(":")[0])
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


# --- JVM: Maven and Gradle (Spring Boot, Quarkus) ------------------------------------------

_JVM_DATABASES = (
    (("postgresql", "r2dbc-postgresql"), "postgres"),
    (("mysql-connector-j", "mysql-connector-java", "mariadb-java-client"), "mysql"),
    (("spring-boot-starter-data-redis", "jedis", "lettuce-core", "quarkus-redis-client"), "redis"),
    (("mongodb-driver-sync", "spring-boot-starter-data-mongodb"), "other"),
)
_JVM_PORT = 8080  # Spring Boot and Quarkus both serve on 8080
_XML_TAG = r"<{0}>\s*([^<\s]+)\s*</{0}>"


def _xml_values(text: str, tag: str) -> list[str]:
    return re.findall(_XML_TAG.format(re.escape(tag)), text)


def _pom_own(text: str) -> str:
    """The pom without the blocks whose artifactId and version are not the project's."""
    for block in ("parent", "dependencies", "dependencyManagement", "build", "profiles"):
        text = re.sub(rf"<{block}>.*?</{block}>", "", text, flags=re.S)
    return text


def _jvm_framework(text: str) -> str | None:
    if "org.springframework.boot" in text:
        return "spring"
    if "io.quarkus" in text:
        return "quarkus"
    return None


def _jvm_common(
    view: RepoView, found: Detection, rel: str, framework: str | None, deps: set[str]
) -> None:
    """Framework, health path, port (convention, then the app's config), and database."""
    found.set("language", "java", DERIVED, rel, 1, f"{rel} present")
    if framework is not None:
        needle = "spring-boot" if framework == "spring" else "io.quarkus"
        line = _line_of(view, rel, needle)
        found.set("framework", framework, DERIVED, rel, line, f"{framework} build")
        found.set("port", _JVM_PORT, DEFAULT, rel, line, f"{framework} serves on {_JVM_PORT}")
    if framework == "spring" and "spring-boot-starter-actuator" in deps:
        line = _dep_line(view, rel, "spring-boot-starter-actuator")
        found.set("health_path", "/actuator/health", DERIVED, rel, line, "Spring Boot Actuator")
    if framework == "quarkus" and "quarkus-smallrye-health" in deps:
        line = _dep_line(view, rel, "quarkus-smallrye-health")
        found.set("health_path", "/q/health", DERIVED, rel, line, "SmallRye Health")
    key = {"spring": "server.port", "quarkus": "quarkus.http.port"}.get(framework or "")
    if key is not None:
        _jvm_config_port(view, found, key)
    _database_from_deps(view, found, rel, deps, _JVM_DATABASES)


def _jvm_config_port(view: RepoView, found: Detection, key: str) -> None:
    base = "src/main/resources/application"
    props = f"{base}.properties"
    for number, text in enumerate(view.lines(props) or [], start=1):
        match = re.match(rf"\s*{re.escape(key)}\s*[=:]\s*(\d{{2,5}})\s*$", text)
        if match:
            found.set("port", int(match.group(1)), EXPLICIT, props, number, key)
            return
    parent = key.rsplit(".", 1)[0].split(".")[-1]  # server, or http under quarkus
    for rel in (f"{base}.yml", f"{base}.yaml"):
        section = None
        for number, text in enumerate(view.lines(rel) or [], start=1):
            heading = re.match(r"^\s*([\w.-]+):\s*$", text)
            if heading:
                section = heading.group(1)
                continue
            match = re.match(r"^\s+port:\s*(\d{2,5})\s*$", text)
            if match and section == parent:
                found.set("port", int(match.group(1)), EXPLICIT, rel, number, key)
                return


def _java_version(view: RepoView, found: Detection, rel: str, text: str) -> None:
    for pattern in (
        r"<java\.version>\s*(\d+(?:\.\d+)?)",
        r"<maven\.compiler\.release>\s*(\d+)",
        r"<maven\.compiler\.source>\s*(\d+(?:\.\d+)?)",
        r"JavaLanguageVersion\.of\(\s*(\d+)\s*\)",
        r"sourceCompatibility\s*=\s*(?:JavaVersion\.VERSION_|['\"])(\d+)",
    ):
        match = re.search(pattern, text)
        if match:
            line = view.find(rel, match.group(0)) or 1
            found.set("runtime_version", match.group(1), EXPLICIT, rel, line, "Java version")
            return


def _maven(view: RepoView, found: Detection) -> None:
    rel = "pom.xml"
    text = view.text(rel) or ""
    deps = set(_xml_values(text, "artifactId"))
    framework = _jvm_framework(text)
    _jvm_common(view, found, rel, framework, deps)
    mvn = "./mvnw" if view.exists("mvnw") else "mvn"
    found.set("package_manager", "maven", DERIVED, rel, 1, "pom.xml")
    found.set("install_cmd", f"{mvn} -B dependency:go-offline", DERIVED, rel, 1, "Maven")
    found.set("build_cmd", f"{mvn} -B package -DskipTests", DERIVED, rel, 1, "Maven package")
    _java_version(view, found, rel, text)
    if framework == "quarkus":
        line = _line_of(view, rel, "io.quarkus")
        command = "java -jar target/quarkus-app/quarkus-run.jar"
        found.set("start_cmd", command, DERIVED, rel, line, "Quarkus fast-jar layout")
        return
    if framework != "spring":
        return
    final = _xml_values(text, "finalName")
    own = _pom_own(text)
    artifact, version = _xml_values(own, "artifactId"), _xml_values(own, "version")
    if final:
        jar, line = final[0], _line_of(view, rel, "<finalName>")
    elif artifact and version:
        jar, line = f"{artifact[0]}-{version[0]}", _line_of(view, rel, f">{artifact[0]}<")
    else:
        return
    found.set("start_cmd", f"java -jar target/{jar}.jar", DERIVED, rel, line, "Spring Boot jar")


def _gradle_file(view: RepoView) -> str | None:
    return next((f for f in ("build.gradle.kts", "build.gradle") if view.exists(f)), None)


_GRADLE_DEP = re.compile(r"""['"]([\w.-]+):([\w.-]+)(?::[^'"]*)?['"]""")


def _gradle(view: RepoView, found: Detection, rel: str) -> None:
    text = view.text(rel) or ""
    deps = {m.group(2) for m in _GRADLE_DEP.finditer(text)}
    framework = _jvm_framework(text)
    _jvm_common(view, found, rel, framework, deps)
    gradle = "./gradlew" if view.exists("gradlew") else "gradle"
    found.set("package_manager", "gradle", DERIVED, rel, 1, rel)
    found.set("build_cmd", f"{gradle} build -x test", DERIVED, rel, 1, "Gradle build")
    _java_version(view, found, rel, text)
    if framework == "quarkus":
        line = _line_of(view, rel, "io.quarkus")
        command = "java -jar build/quarkus-app/quarkus-run.jar"
        found.set("start_cmd", command, DERIVED, rel, line, "Quarkus fast-jar layout")
        return
    if framework != "spring":
        return
    settings = next((f for f in ("settings.gradle.kts", "settings.gradle") if view.exists(f)), None)
    if settings is None:
        return
    name = re.search(r"""rootProject\.name\s*=\s*['"]([\w.-]+)['"]""", view.text(settings) or "")
    if name is None:
        return
    version = re.search(r"""^version\s*=\s*['"]([\w.+-]+)['"]""", text, re.MULTILINE)
    jar = f"{name.group(1)}-{version.group(1)}" if version else name.group(1)
    line = _line_of(view, settings, "rootProject.name")
    found.set("start_cmd", f"java -jar build/libs/{jar}.jar", DERIVED, settings, line, "boot jar")


# --- Ruby (Rails, Sinatra, Rack) ------------------------------------------------------------

_GEM = re.compile(r"""^\s*gem\s+['"]([\w.-]+)['"]""")
_RUBY_DATABASES = (
    (("pg",), "postgres"),
    (("mysql2", "trilogy"), "mysql"),
    (("redis",), "redis"),
    (("mongoid", "mongo"), "other"),
)
_RACKUP = "bundle exec rackup --host 0.0.0.0 --port 9292"


def _ruby(view: RepoView, found: Detection) -> None:
    rel = "Gemfile"
    gems: dict[str, int] = {}
    for number, text in enumerate(view.lines(rel) or [], start=1):
        match = _GEM.match(text)
        if match:
            gems.setdefault(match.group(1), number)
    found.set("language", "ruby", DERIVED, rel, 1, "Gemfile present")
    if view.exists("Gemfile.lock"):
        found.set("package_manager", "bundler", EXPLICIT, "Gemfile.lock", 1, "lockfile")
    else:
        found.set("package_manager", "bundler", DERIVED, rel, 1, "Gemfile without a lockfile")
    found.set("install_cmd", "bundle install", DERIVED, rel, 1, "bundler")
    _ruby_version(view, found)

    if "rails" in gems:
        line = gems["rails"]
        found.set("framework", "rails", DERIVED, rel, line, "rails gem")
        command = "bundle exec rails server -b 0.0.0.0 -p 3000"
        found.set("start_cmd", command, DERIVED, rel, line, "Rails server")
        found.set("port", 3000, DEFAULT, rel, line, "Rails serves on 3000")
        if any(f.startswith("app/assets/") for f in view.files):
            command = "bundle exec rails assets:precompile"
            found.set("build_cmd", command, DERIVED, rel, line, "Rails assets")
        routes = "config/routes.rb"
        for number, text in enumerate(view.lines(routes) or [], start=1):
            match = re.search(r"""get\s+['"]/?([\w/-]*)['"].*rails/health#show""", text)
            if match:
                path = "/" + match.group(1)
                found.set("health_path", path, EXPLICIT, routes, number, "Rails health route")
                break
    elif view.exists("config.ru"):
        framework = "sinatra" if "sinatra" in gems else "rack"
        line = gems.get(framework, 1)
        found.set("framework", framework, DERIVED, rel, line, f"{framework} with config.ru")
        found.set("start_cmd", _RACKUP, DERIVED, "config.ru", 1, "rackup file")
        found.set("port", 9292, DEFAULT, "config.ru", 1, "rackup serves on 9292")
    elif "sinatra" in gems:
        found.set("framework", "sinatra", DERIVED, rel, gems["sinatra"], "sinatra gem")
        for source in view.source_files:
            if not source.endswith(".rb"):
                continue
            found_at = view.find(source, "require 'sinatra") or view.find(
                source, 'require "sinatra'
            )
            if found_at:
                command = f"bundle exec ruby {source} -o 0.0.0.0 -p 4567"
                found.set("start_cmd", command, DERIVED, source, found_at, "Sinatra app file")
                found.set("port", 4567, DEFAULT, source, found_at, "Sinatra serves on 4567")
                break
    _database_from_deps(view, found, rel, set(gems), _RUBY_DATABASES)


def _ruby_version(view: RepoView, found: Detection) -> None:
    text = (view.text(".ruby-version") or "").strip().removeprefix("ruby-")
    if re.fullmatch(r"\d+\.\d+(?:\.\d+)?", text):
        found.set("runtime_version", text, EXPLICIT, ".ruby-version", 1, ".ruby-version")
        return
    for number, line in enumerate(view.lines("Gemfile") or [], start=1):
        match = re.match(r"""\s*ruby\s+['"][^\d'"]*(\d+\.\d+(?:\.\d+)?)""", line)
        if match:
            found.set("runtime_version", match.group(1), EXPLICIT, "Gemfile", number, "ruby")
            return


# --- Rust (axum, actix-web, Rocket) ---------------------------------------------------------

_RUST_FRAMEWORKS = (
    # crate, framework, the port its examples and docs use
    ("axum", "axum", 3000),
    ("actix-web", "actix", 8080),
    ("rocket", "rocket", 8000),
)
_RUST_DATABASES = (
    (
        ("tokio-postgres", "postgres", "sqlx:postgres", "diesel:postgres", "sea-orm:sqlx-postgres"),
        "postgres",
    ),
    (("mysql", "mysql_async", "sqlx:mysql", "diesel:mysql", "sea-orm:sqlx-mysql"), "mysql"),
    (("redis",), "redis"),
    (("mongodb",), "other"),
)


def _rust(view: RepoView, found: Detection) -> None:
    rel = "Cargo.toml"
    try:
        data = tomllib.loads(view.text(rel) or "")
    except tomllib.TOMLDecodeError:
        found.notes.append("Cargo.toml is not valid TOML")
        return
    found.set("language", "rust", DERIVED, rel, 1, "Cargo.toml present")
    found.set("package_manager", "cargo", DERIVED, rel, 1, "cargo")
    found.set("install_cmd", "cargo fetch", DERIVED, rel, 1, "cargo")
    locked = " --locked" if view.exists("Cargo.lock") else ""
    found.set("build_cmd", f"cargo build --release{locked}", DERIVED, rel, 1, "cargo build")
    package = data.get("package") or {}
    bins = [b for b in data.get("bin") or [] if isinstance(b, dict) and b.get("name")]
    binary = str(bins[0]["name"]) if bins else package.get("name")
    if binary:
        line = _line_of(view, rel, f'"{binary}"')
        found.set("start_cmd", f"./target/release/{binary}", DERIVED, rel, line, "the binary")
    rust_version = package.get("rust-version")
    if isinstance(rust_version, str):
        line = _line_of(view, rel, "rust-version")
        found.set("runtime_version", rust_version, EXPLICIT, rel, line, "rust-version")
    toolchain = re.search(
        r"""channel\s*=\s*['"](\d+\.\d+(?:\.\d+)?)['"]""", view.text("rust-toolchain.toml") or ""
    )
    if toolchain:
        where = "rust-toolchain.toml"
        line = _line_of(view, where, "channel")
        found.set("runtime_version", toolchain.group(1), EXPLICIT, where, line, "toolchain")
    deps: set[str] = set()
    for name, spec in (data.get("dependencies") or {}).items():
        deps.add(name)
        features = spec.get("features", []) if isinstance(spec, dict) else []
        deps.update(f"{name}:{f}" for f in features)
    for crate, framework, port in _RUST_FRAMEWORKS:
        if crate in deps:
            line = _dep_line(view, rel, crate)
            found.set("framework", framework, DERIVED, rel, line, f"{crate} dependency")
            found.set("port", port, DEFAULT, rel, line, f"{framework} apps serve on {port}")
            break
    _database_from_deps(view, found, rel, deps, _RUST_DATABASES)


# --- PHP (Laravel, Slim, plain) -------------------------------------------------------------

_PHP_DATABASES = (
    (("ext-pgsql", "ext-pdo_pgsql"), "postgres"),
    (("ext-mysqli", "ext-pdo_mysql"), "mysql"),
    (("predis/predis", "ext-redis"), "redis"),
    (("mongodb/mongodb",), "other"),
)
# The Dockerfile Builder writes for PHP is php:<v>-apache: Apache serves on 80 and runs PHP
# as www-data.
PHP_PORT = 80
_PHP_START = "apache2-foreground"


def _plain_php_entry(view: RepoView) -> str | None:
    return next((f for f in ("index.php", "public/index.php") if view.exists(f)), None)


def _php(view: RepoView, found: Detection) -> None:
    rel = "composer.json" if view.exists("composer.json") else _plain_php_entry(view)
    if rel is None:
        return
    found.set("language", "php", DERIVED, rel, 1, f"{rel} present")
    found.set("start_cmd", _PHP_START, DEFAULT, rel, 1, "php-apache image")
    found.set("port", PHP_PORT, DEFAULT, rel, 1, f"php-apache serves on {PHP_PORT}")
    if rel != "composer.json":
        return
    try:
        data = json.loads(view.text(rel) or "{}")
    except json.JSONDecodeError:
        found.notes.append("composer.json is not valid JSON")
        return
    require: dict[str, Any] = (data.get("require") or {}) if isinstance(data, dict) else {}
    if view.exists("composer.lock"):
        found.set("package_manager", "composer", EXPLICIT, "composer.lock", 1, "lockfile")
    else:
        found.set("package_manager", "composer", DERIVED, rel, 1, "composer.json")
    command = "composer install --no-dev --optimize-autoloader --no-interaction"
    found.set("install_cmd", command, DERIVED, rel, 1, "composer")
    version = re.search(r"\d+\.\d+", str(require.get("php", "")))
    if version:
        line = _line_of(view, rel, '"php"')
        found.set("runtime_version", version.group(0), EXPLICIT, rel, line, "require.php")
    for package, framework in (("laravel/framework", "laravel"), ("slim/slim", "slim")):
        if package in require:
            line = _dep_line(view, rel, package)
            found.set("framework", framework, DERIVED, rel, line, f"{package} dependency")
            break
    boot = "bootstrap/app.php"
    for number, text in enumerate(view.lines(boot) or [], start=1):
        match = re.search(r"""health:\s*['"](/[^'"]*)['"]""", text)
        if match:
            found.set("health_path", match.group(1), EXPLICIT, boot, number, "Laravel health")
            break
    _database_from_deps(view, found, rel, set(require), _PHP_DATABASES)


# --- .NET (ASP.NET Core) --------------------------------------------------------------------

_WEB_SDK = "Microsoft.NET.Sdk.Web"
_DOTNET_DATABASES = (
    (("Npgsql", "Npgsql.EntityFrameworkCore.PostgreSQL"), "postgres"),
    (("MySqlConnector", "MySql.Data", "Pomelo.EntityFrameworkCore.MySql"), "mysql"),
    (("StackExchange.Redis", "Microsoft.Extensions.Caching.StackExchangeRedis"), "redis"),
    (("MongoDB.Driver",), "other"),
)


def _dotnet_project(view: RepoView) -> str | None:
    """The project to publish: the web project (ASP.NET Core), else the only project.
    Projects up to three folders deep (a solution at the root, src/Api/Api.csproj)."""
    projects = [f for f in view.files if f.endswith((".csproj", ".fsproj")) and f.count("/") <= 3]
    web = [p for p in projects if _WEB_SDK in (view.text(p) or "")]
    if web:
        return sorted(web, key=lambda p: (p.count("/"), p))[0]
    return projects[0] if len(projects) == 1 else None


def _dotnet(view: RepoView, found: Detection, rel: str) -> None:
    text = view.text(rel) or ""
    found.set("language", "dotnet", DERIVED, rel, 1, f"{rel} present")
    found.set("package_manager", "dotnet", DERIVED, rel, 1, "NuGet through dotnet")
    found.set("install_cmd", f"dotnet restore {rel}", DERIVED, rel, 1, "dotnet restore")
    command = f"dotnet publish {rel} -c Release -o out"
    found.set("build_cmd", command, DERIVED, rel, 1, "dotnet publish")
    assembly = _xml_values(text, "AssemblyName")
    name = assembly[0] if assembly else PurePosixPath(rel).stem
    line = _line_of(view, rel, "<AssemblyName>") if assembly else 1
    found.set("start_cmd", f"dotnet {name}.dll", DERIVED, rel, line, "published assembly")
    target = re.search(r"<TargetFrameworks?>\s*net(\d+)\.(\d+)", text)
    major = int(target.group(1)) if target else None
    if target:
        line = _line_of(view, rel, "<TargetFramework")
        version = f"{target.group(1)}.{target.group(2)}"
        found.set("runtime_version", version, EXPLICIT, rel, line, "TargetFramework")
    if _WEB_SDK in text:
        line = _line_of(view, rel, _WEB_SDK)
        found.set("framework", "aspnet", DERIVED, rel, line, "web SDK")
        # .NET 8 images listen on 8080 (ASPNETCORE_HTTP_PORTS); before that, on 80.
        port = 80 if major is not None and major < 8 else 8080
        found.set("port", port, DEFAULT, rel, line, f"ASP.NET Core serves on {port}")
    packages = set(re.findall(r"""<PackageReference\s+Include=["']([^"']+)["']""", text))
    _database_from_deps(view, found, rel, packages, _DOTNET_DATABASES)


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
_ROUTE_HINT = re.compile(
    r"\.(get|route|use|all|handle|HandleFunc|GET)\b|@\w+\.(get|route)|@(Get|Request)Mapping|"
    r"@Path\b|Map(Get|HealthChecks)\b|Route::get|#\[get\b",
    re.I,
)
_LISTEN_PORT = re.compile(
    r"\.listen\(\s*(\d{2,5})|PORT\s*(?:\|\||\?\?)\s*(\d{2,5})|port\s*=\s*(\d{2,5})|\"(?::|0\.0\.0\.0:)(\d{2,5})\""
    r"|\(\s*\"0\.0\.0\.0\"\s*,\s*(\d{2,5})\s*\)"
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
            if port and any(w in text for w in ("listen", "PORT", "run(", "Run(", "bind(")):
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
