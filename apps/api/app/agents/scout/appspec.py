"""AppSpec: what Scout learns about an app, and where each value came from.

Every field the code compares on is a lowercase Literal that includes "other" (the LLM client
refuses schemas without it, because strict json_schema forces a value from the list even when
none fits). Free-text fields (commands, versions, paths) stay strings.
"""

from typing import Literal

from pydantic import BaseModel, Field

Language = Literal[
    "javascript", "typescript", "python", "go", "java", "ruby", "php", "rust", "dotnet", "other"
]
Framework = Literal[
    "nextjs",
    "express",
    "nestjs",
    "fastify",
    "react",
    "vite",
    "nuxt",
    "sveltekit",
    "remix",
    "fastapi",
    "flask",
    "django",
    "gin",
    "echo",
    "fiber",
    "spring",
    "quarkus",
    "rails",
    "sinatra",
    "rack",
    "axum",
    "actix",
    "rocket",
    "laravel",
    "slim",
    "aspnet",
    "none",
    "other",
]
PackageManager = Literal[
    "npm",
    "yarn",
    "pnpm",
    "bun",
    "pip",
    "poetry",
    "uv",
    "pipenv",
    "go",
    "maven",
    "gradle",
    "bundler",
    "cargo",
    "composer",
    "dotnet",
    "other",
]
Database = Literal["none", "postgres", "mysql", "redis", "other"]
Source = Literal["rule", "model"]

# Fields Scout fills, in the order they are shown.
SPEC_FIELDS = (
    "language",
    "framework",
    "runtime_version",
    "package_manager",
    "install_cmd",
    "build_cmd",
    "start_cmd",
    "port",
    "database",
    "health_path",
    "env_vars",
)
FieldName = Literal[
    "language",
    "framework",
    "runtime_version",
    "package_manager",
    "install_cmd",
    "build_cmd",
    "start_cmd",
    "port",
    "database",
    "health_path",
    "env_vars",
    "has_dockerfile",
    "subpath",
    "other",
]


class EnvVar(BaseModel):
    name: str
    required: bool = True
    secret: bool = False
    example: str | None = None


class Evidence(BaseModel):
    """Why a field has its value: a file and line in the repository, and a short reason."""

    field: FieldName
    file: str
    line: int = Field(ge=1)
    reason: str

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}"


class Conflict(BaseModel):
    """The rules and the model disagreed. The rule value is kept."""

    field: str
    rule_value: str
    model_value: str


class Unverified(BaseModel):
    """A model value no evidence line supports. Not applied to the spec; shown so a person (or
    a later step that can check it, like a build) can decide."""

    field: str
    value: str


class AppSpec(BaseModel):
    language: Language | None = None
    framework: Framework | None = None
    runtime_version: str | None = None
    package_manager: PackageManager | None = None
    install_cmd: str | None = None
    build_cmd: str | None = None
    start_cmd: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    env_vars: list[EnvVar] = []
    database: Database | None = None
    health_path: str | None = None
    has_dockerfile: bool = False
    # The repository's Dockerfile, relative to the app folder, when it is not "Dockerfile":
    # a Compose service's `build.dockerfile` (Dockerfile.prod, docker/api.Dockerfile).
    dockerfile: str | None = None
    subpath: str | None = None
    # 0 to 1, computed from what is known and how well it is supported (not the model's word).
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: list[Evidence] = []
    # Where each known field's value came from: a detector rule or the model.
    sources: dict[str, Source] = {}
    conflicts: list[Conflict] = []
    # Fields where the model gave a value that no evidence line supports. The value is NOT
    # applied (the field stays unknown); it is listed in `unverified`. Env vars: "env_vars.NAME".
    low_confidence: list[str] = []
    unverified: list[Unverified] = []


class DraftEnvVar(BaseModel):
    name: str
    required: bool | None = None
    secret: bool | None = None
    example: str | None = None


class ScoutDraft(BaseModel):
    """What Scout asks Nemotron Nano to return (strict JSON schema). Unknown = null."""

    language: Language | None = None
    framework: Framework | None = None
    runtime_version: str | None = None
    package_manager: PackageManager | None = None
    install_cmd: str | None = None
    build_cmd: str | None = None
    start_cmd: str | None = None
    port: int | None = None
    database: Database | None = None
    health_path: str | None = None
    env_vars: list[DraftEnvVar] = []
    evidence: list[Evidence] = []
