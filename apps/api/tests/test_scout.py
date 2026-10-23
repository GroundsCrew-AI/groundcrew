"""Scout: detectors, the merge rules, the prompt cap, and golden fixtures with a scripted LLM."""

import json
from pathlib import Path
from typing import Any

import pytest

from app.agents.scout.accuracy import field_matches, load_golden
from app.agents.scout.appspec import Evidence, ScoutDraft
from app.agents.scout.detectors import (
    detect,
    find_subpath,
    is_secret_name,
    service_dirs,
    service_markers,
)
from app.agents.scout.repo_view import RepoView
from app.agents.scout.scout import PROMPT_TOKEN_CAP, build_prompt, merge, scout
from app.agents.scout.summary import ResourceSummary, summarize_system
from app.config import Settings
from app.llm.budget import MemoryUsageStore, RunBudget
from app.llm.client import LLMError
from tests.fakes import FakeLLM, reply

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).resolve().parents[3] / "demo" / "fixtures"
FIXTURE_NAMES = (
    "next-app",
    "fastapi-pg",
    "express-redis",
    "py-build-trap",
    "java-spring-maven",
    "ruby-rails",
    "rust-axum",
    "php-laravel",
    "dotnet-aspnet",
)


def _settings() -> Settings:
    return Settings(_env_file=None, model_nano="m/nano", model_super="m/super")


def _budget() -> RunBudget:
    return RunBudget(usage=MemoryUsageStore(), settings=_settings())


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _ev(field: str, file: str, line: int, reason: str = "seen") -> dict[str, Any]:
    return {"field": field, "file": file, "line": line, "reason": reason}


def _draft_reply(**fields: Any) -> Any:
    return reply(json.dumps({"evidence": [], "env_vars": [], **fields}))


# --- detectors -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_rules_alone_match_the_golden_spec(name: str) -> None:
    view = RepoView(FIXTURES / name / "repo")
    spec = merge(view, detect(view), None)
    matches = field_matches(spec, load_golden(FIXTURES / name))
    assert all(matches.values()), matches
    assert set(spec.sources.values()) == {"rule"}
    # Every rule value points at a real line.
    for item in spec.evidence:
        assert view.line(item.file, item.line) is not None, item


def test_dependency_evidence_points_at_the_dependency_line() -> None:
    view = RepoView(FIXTURES / "express-redis" / "repo")
    found = detect(view)
    assert found.values["database"].evidence.location == "package.json:10"  # not the name line
    assert '"redis"' in (view.line("package.json", 10) or "")


def test_lockfiles_procfile_dockerfile_and_scripts(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "package.json",
        json.dumps(
            {
                "scripts": {"build": "vite build", "start": "node dist/server.js --port 4000"},
                "dependencies": {"fastify": "4", "ioredis": "5"},
            },
            indent=1,
        ),
    )
    _write(tmp_path, "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
    _write(tmp_path, "Procfile", "web: node dist/server.js --port 5000\n")
    _write(tmp_path, "Dockerfile", "FROM node:20\nEXPOSE 5000\n")
    found = detect(RepoView(tmp_path))
    v = {k: f.value for k, f in found.values.items()}
    assert v["package_manager"] == "pnpm" and v["install_cmd"] == "pnpm install --frozen-lockfile"
    assert v["build_cmd"] == "pnpm run build"
    assert v["start_cmd"] == "node dist/server.js --port 5000"  # the Procfile is explicit
    assert v["port"] == 5000 and v["framework"] == "fastify" and v["database"] == "redis"
    assert found.has_dockerfile is not None


def test_python_variants(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "pyproject.toml",
        '[project]\nrequires-python = ">=3.11"\ndependencies = ["flask>=3", "pymysql"]\n',
    )
    _write(tmp_path, "uv.lock", "version = 1\n")
    _write(tmp_path, "web/server.py", "from flask import Flask\napplication = Flask(__name__)\n")
    v = {k: f.value for k, f in detect(RepoView(tmp_path)).values.items()}
    assert (v["package_manager"], v["install_cmd"]) == ("uv", "uv sync --frozen --no-dev")
    assert v["runtime_version"] == "3.11" and v["database"] == "mysql"
    assert v["start_cmd"] == "flask --app web.server:application run --host 0.0.0.0 --port 5000"
    assert v["port"] == 5000


def test_go_module(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "go.mod",
        "module example.com/api\n\ngo 1.22\n\nrequire (\n"
        "\tgithub.com/gin-gonic/gin v1.10.0\n\tgithub.com/jackc/pgx/v5 v5.6.0\n)\n",
    )
    _write(tmp_path, "main.go", 'package main\nfunc main() { r.Run(":9090") }\n')
    v = {k: f.value for k, f in detect(RepoView(tmp_path)).values.items()}
    assert (v["language"], v["framework"], v["runtime_version"]) == ("go", "gin", "1.22")
    assert (v["database"], v["port"], v["start_cmd"]) == ("postgres", 9090, "./server")


def test_env_vars_required_comes_from_code(tmp_path: Path) -> None:
    _write(tmp_path, "requirements.txt", "fastapi\n")
    _write(tmp_path, ".env.example", "API_TOKEN=\nLOG_LEVEL=info\nFEATURE_X=1\n")
    _write(
        tmp_path,
        "main.py",
        'import os\nlevel = os.getenv("LOG_LEVEL", "info")\ntoken = os.environ["API_TOKEN"]\n',
    )
    env = {n: v for n, (v, _) in detect(RepoView(tmp_path)).env_vars.items()}
    assert (env["API_TOKEN"].required, env["API_TOKEN"].secret) == (True, True)
    assert (env["LOG_LEVEL"].required, env["LOG_LEVEL"].example) == (False, "info")
    assert env["FEATURE_X"].required  # only in the template: assume required


def test_secret_detection() -> None:
    for name in ("DATABASE_URL", "STRIPE_SECRET_KEY", "GITHUB_TOKEN", "DB_PASSWORD", "SENTRY_DSN"):
        assert is_secret_name(name), name
    for name in ("PORT", "LOG_LEVEL", "NODE_ENV", "APP_URL"):
        assert not is_secret_name(name), name


def test_monorepo_subpath(tmp_path: Path) -> None:
    _write(tmp_path, "apps/api/requirements.txt", "fastapi\n")
    assert find_subpath(RepoView(tmp_path)) == ("apps/api", ["apps/api"])
    _write(tmp_path, "apps/web/package.json", "{}")
    assert find_subpath(RepoView(tmp_path)) == (None, ["apps/api", "apps/web"])
    _write(tmp_path, "package.json", "{}")
    assert find_subpath(RepoView(tmp_path)) == (None, [])


# --- prompt ----------------------------------------------------------------------------------


def test_prompt_is_capped_and_never_contains_secrets(tmp_path: Path) -> None:
    _write(tmp_path, "requirements.txt", "flask\n")
    _write(tmp_path, ".env", "DATABASE_URL=postgres://u:hunter2secret@db/app\n")
    _write(tmp_path, "ignored.py", "x = 1\n")
    _write(tmp_path, ".gitignore", "ignored.py\n")
    for i in range(400):
        _write(tmp_path, f"pkg/mod_{i:03}.py", "\n".join(f"x{j} = {j}" for j in range(80)))
    _write(tmp_path, "app.py", "from flask import Flask\napp = Flask(__name__)\n" + "y = 1\n" * 300)
    view = RepoView(tmp_path)
    messages = build_prompt(view, detect(view))
    total = sum(len(m["content"]) for m in messages)
    assert total <= PROMPT_TOKEN_CAP * 4
    text = json.dumps(messages)
    assert "hunter2secret" not in text and "ignored.py" not in text
    assert "[... " in messages[1]["content"]  # the file list says it was cut


# --- merge rules -----------------------------------------------------------------------------


def _view_with_app(tmp_path: Path) -> RepoView:
    _write(tmp_path, "requirements.txt", "flask\n")
    _write(tmp_path, "app.py", "from flask import Flask\napp = Flask(__name__)\n\nPORT = 7000\n")
    _write(tmp_path, "worker.py", 'import os\nQUEUE = os.environ.get("QUEUE_URL")\n')
    return RepoView(tmp_path)


def test_rule_beats_model_and_the_conflict_is_recorded(tmp_path: Path) -> None:
    view = _view_with_app(tmp_path)
    draft = ScoutDraft(framework="django", language="python")
    spec = merge(view, detect(view), draft)
    assert spec.framework == "flask" and spec.sources["framework"] == "rule"
    assert [c.model_dump() for c in spec.conflicts] == [
        {"field": "framework", "rule_value": "flask", "model_value": "django"}
    ]


def test_supported_model_value_is_accepted(tmp_path: Path) -> None:
    view = _view_with_app(tmp_path)
    draft = ScoutDraft.model_validate(
        {"runtime_version": "3.12", "evidence": [_ev("runtime_version", "app.py", 1)]}
    )
    spec = merge(view, detect(view), draft)
    # "3.12" is not on app.py:1, so it is not supported: not applied, listed as unverified.
    assert spec.runtime_version is None and spec.low_confidence == ["runtime_version"]
    assert [u.model_dump() for u in spec.unverified] == [
        {"field": "runtime_version", "value": "3.12"}
    ]
    _write(tmp_path, "runtime.notes", "Runs on Python 3.12\n")
    view = RepoView(tmp_path)
    draft = ScoutDraft.model_validate(
        {"runtime_version": "3.12", "evidence": [_ev("runtime_version", "runtime.notes", 1)]}
    )
    spec = merge(view, detect(view), draft)
    assert spec.runtime_version == "3.12"
    assert spec.sources["runtime_version"] == "model" and spec.low_confidence == []
    assert any(e.location == "runtime.notes:1" for e in spec.evidence)


@pytest.mark.parametrize(
    "evidence",
    [
        [],  # no evidence at all
        [_ev("health_path", "app.py", 99)],  # a line that does not exist
        [_ev("health_path", "nope.py", 1)],  # a file that does not exist
        [_ev("health_path", ".env", 1)],  # a file Scout may not read
    ],
)
def test_unsupported_model_value_is_low_confidence(tmp_path: Path, evidence: list[Any]) -> None:
    view = _view_with_app(tmp_path)
    _write(tmp_path, ".env", "HEALTH=/health\n")
    draft = ScoutDraft.model_validate({"health_path": "/health", "evidence": evidence})
    spec = merge(RepoView(tmp_path), detect(view), draft)
    assert spec.health_path is None and "health_path" not in spec.sources
    assert spec.low_confidence == ["health_path"]
    assert spec.unverified[0].value == "/health"
    assert spec.confidence < merge(view, detect(view), None).confidence + 0.0001


def test_model_env_vars(tmp_path: Path) -> None:
    view = _view_with_app(tmp_path)
    draft = ScoutDraft.model_validate(
        {
            "env_vars": [
                {"name": "QUEUE_URL", "required": True},  # rule already has it: rule wins
                {"name": "SENTRY_DSN"},
                {"name": "MADE_UP"},
            ],
            "evidence": [_ev("env_vars", "worker.py", 2), _ev("env_vars", "app.py", 4)],
        }
    )
    _write(
        tmp_path,
        "app.py",
        "import os\nfrom flask import Flask\napp = Flask(__name__)\n"
        'dsn = os.environ.get("SENTRY" + "_DSN")  # SENTRY_DSN\n',
    )
    view = RepoView(tmp_path)
    spec = merge(view, detect(view), draft)
    env = {v.name: v for v in spec.env_vars}
    assert env["QUEUE_URL"].required is True  # .get() with no default: required
    assert spec.sources["env_vars.QUEUE_URL"] == "rule"
    assert spec.sources["env_vars.SENTRY_DSN"] == "model" and env["SENTRY_DSN"].secret
    assert spec.low_confidence == ["env_vars.MADE_UP"] and "MADE_UP" not in env


# --- the full Scout with a scripted Nano -------------------------------------------------------


@pytest.mark.parametrize("name", FIXTURE_NAMES)
async def test_golden_fixtures_with_a_wrong_model(name: str) -> None:
    """The model answers confidently wrong (like strict enums forcing python/javascript):
    the rules keep the golden values and every disagreement is recorded."""
    golden = load_golden(FIXTURES / name)
    llm = FakeLLM(
        _draft_reply(language="other", framework="django", port=1234, database="mysql"),
        settings=_settings(),
    )
    result = await scout(FIXTURES / name / "repo", llm, _budget())
    assert all(field_matches(result.spec, golden).values()), field_matches(result.spec, golden)
    assert {c.field for c in result.spec.conflicts} == {"language", "framework", "port", "database"}
    assert result.model_used and result.spec.confidence >= 0.8


async def test_model_fills_a_gap_with_evidence(tmp_path: Path) -> None:
    _write(tmp_path, "requirements.txt", "flask\n")
    _write(tmp_path, "app.py", "from flask import Flask\napp = Flask(__name__)\n")
    _write(tmp_path, "README.md", "# Demo\n\nHealth check: GET /status\n")
    llm = FakeLLM(
        _draft_reply(health_path="/status", evidence=[_ev("health_path", "README.md", 3)]),
        settings=_settings(),
    )
    result = await scout(tmp_path, llm, _budget())
    assert result.spec.health_path == "/status"
    assert result.spec.sources["health_path"] == "model" and result.spec.low_confidence == []
    # Routed to detect_app_spec: Nano with reasoning low, strict schema.
    assert llm.models == ["m/nano"]
    assert llm.reasoning_kwargs == [{"enable_thinking": True, "low_effort": True}]
    assert llm.calls[0]["response_format"]["json_schema"]["name"] == "ScoutDraft"
    assert result.prompt_tokens_estimate <= PROMPT_TOKEN_CAP


async def test_model_enum_values_are_normalized(tmp_path: Path) -> None:
    _write(tmp_path, "main.go", "package main\n")
    _write(tmp_path, "notes.txt", "Written in Golang with Gin\n")
    llm = FakeLLM(
        _draft_reply(
            language="Go",
            framework="Gin",
            evidence=[_ev("language", "main.go", 1), _ev("framework", "notes.txt", 1)],
        ),
        settings=_settings(),
    )
    spec = (await scout(tmp_path, llm, _budget())).spec
    assert (spec.language, spec.framework) == ("go", "gin")
    assert spec.low_confidence == []


async def test_model_failure_falls_back_to_rules(tmp_path: Path) -> None:
    llm = FakeLLM(LLMError("Token Factory call failed"), settings=_settings())
    result = await scout(FIXTURES / "fastapi-pg" / "repo", llm, _budget())
    assert not result.model_used and result.notes[0].startswith("Model call failed")
    assert all(field_matches(result.spec, load_golden(FIXTURES / "fastapi-pg")).values())


async def test_rules_only_mode_makes_no_call() -> None:
    llm = FakeLLM(settings=_settings())
    result = await scout(FIXTURES / "next-app" / "repo", llm, _budget(), use_model=False)
    assert llm.calls == [] and result.spec.framework == "nextjs"


async def test_monorepo_subpath_is_used(tmp_path: Path) -> None:
    _write(tmp_path, "services/api/requirements.txt", "fastapi\n")
    _write(tmp_path, "services/api/main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    result = await scout(tmp_path, FakeLLM(settings=_settings()), _budget(), use_model=False)
    assert result.spec.subpath == "services/api"
    assert result.spec.start_cmd == "uvicorn main:app --host 0.0.0.0 --port 8000"


def test_evidence_location() -> None:
    assert Evidence(field="port", file="a.py", line=3, reason="x").location == "a.py:3"


# --- system summary --------------------------------------------------------------------------


async def test_summarize_system() -> None:
    resources = [
        ResourceSummary(provider="aws", type="rds", name="orders-db", region="us-east-1"),
        ResourceSummary(provider="azure", type="app_service", name="api", relations=["orders-db"]),
    ]
    llm = FakeLLM(reply("- api on Azure reads orders-db on AWS"), settings=_settings())
    text = await summarize_system(llm, _budget(), resources)
    assert text == "- api on Azure reads orders-db on AWS"
    assert llm.models == ["m/nano"] and llm.reasoning_kwargs == [{"enable_thinking": False}]
    user = llm.calls[0]["messages"][1]["content"]
    assert user.index('"aws"') < user.index('"azure"')  # sorted, deterministic
    assert await summarize_system(llm, _budget(), []) == "No resources found yet."


def test_service_dirs_across_languages() -> None:
    files = [
        "Makefile",
        "api/pyproject.toml",
        "web/package.json",
        "web/scripts/package.json",  # inside web: part of it
        "worker/go.mod",
        "services/billing/api/pom.xml",
        "services/ledger/Ledger.csproj",
        "api/tests/fixtures/requirements.txt",
        "examples/demo/package.json",
        "node_modules/x/package.json",
        "a/b/c/d/Cargo.toml",  # too deep
        "infra/aws/lambda/handler.py",  # no manifest: not a service
    ]
    assert service_dirs(files) == [
        "api",
        "services/billing/api",
        "services/ledger",
        "web",
        "worker",
    ]
    assert service_dirs(["package.json", "apps/api/go.mod"]) == [".", "apps/api"]


def test_dockerfile_only_folders_are_services() -> None:
    files = [
        "api/go.mod",
        "site/Dockerfile",  # a static site: no manifest, its own image
        "site/index.html",
        "docker/Dockerfile",  # builds another folder: not a service by itself
        "api/Dockerfile",
    ]
    assert service_markers(files) == {
        "api": ["Dockerfile", "go.mod"],
        "site": ["Dockerfile"],
    }


def test_compose_names_its_build_folders() -> None:
    compose = """
services:
  api: {build: ./backend/services/api/server}  # deeper than manifests are looked for
  web:
    build: {context: frontend, dockerfile: Dockerfile.prod}
  db: {image: postgres:16}
  remote: {build: "https://github.com/acme/x.git"}
  escape: {build: ../outside}
  worker: {build: ./frontend/worker}  # inside web, but Compose builds it on its own
"""
    files = ["docker-compose.yml", "frontend/package.json", "README.md"]
    read = {"docker-compose.yml": compose}.get
    assert service_markers(files, read) == {
        "backend/services/api/server": ["docker-compose.yml"],
        "frontend": ["docker-compose.yml", "package.json"],
        "frontend/worker": ["docker-compose.yml"],
    }
    # Without the file's text the manifest is all there is.
    assert service_dirs(files) == ["frontend"]
    # A broken Compose file is ignored.
    assert service_dirs(files, {"docker-compose.yml": "services: [:"}.get) == ["frontend"]


def test_compose_building_the_root_makes_it_the_app(tmp_path: Path) -> None:
    _write(tmp_path, "compose.yaml", "services:\n  app:\n    build: .\n")
    _write(tmp_path, "worker/requirements.txt", "celery\n")
    _write(tmp_path, "admin/package.json", "{}")
    # The root is the app: no service has to be chosen.
    assert find_subpath(RepoView(tmp_path)) == (None, [])


def test_a_dockerfile_only_service_is_described_from_its_dockerfile(tmp_path: Path) -> None:
    _write(tmp_path, "Dockerfile", "FROM nginx:1.27\nCOPY . /usr/share/nginx/html\nEXPOSE 80\n")
    found = detect(RepoView(tmp_path))
    assert found.has_dockerfile is not None
    assert found.value("port") == 80
