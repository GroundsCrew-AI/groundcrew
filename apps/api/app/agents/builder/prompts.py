"""Builder prompts: write a Dockerfile from Scout's AppSpec, or fix one after a failed build.

Prompts stay small (Super reads them fine, but every token costs): the spec, a capped file list,
short excerpts of the files that decide how to build, and for a fix the failing step, the error
lines, and the end of the log.
"""

from pathlib import Path

from app.agents.scout.appspec import AppSpec
from app.agents.scout.repo_view import RepoView

MAX_FILES = 60
EXCERPT_CHARS = 1_500
LOG_CHARS = 3_000
# Files that decide how an app installs, builds, and starts.
BUILD_FILES = (
    "requirements.txt",
    "pyproject.toml",
    "Pipfile",
    "setup.py",
    "Procfile",
    "runtime.txt",
    ".python-version",
    "package.json",
    ".nvmrc",
    "go.mod",
    "Dockerfile",
    "app.py",
    "wsgi.py",
    "main.py",
    "manage.py",
    "server.js",
    "index.js",
)

WRITE_SYSTEM = """You are Builder, part of Groundcrew. You write a Dockerfile that builds and \
runs the app described below, for Linux x86_64, in a sandbox with network access.

Rules:
- Use an official, slim base image that matches the runtime version (for example \
python:3.12-slim, node:22-slim).
- Install dependencies from the app's own manifest (requirements.txt, package.json, go.mod) \
before copying the rest of the code, so the layer caches.
- Start the app with its production server and bind to 0.0.0.0 on the app's port (EXPOSE it). \
No dev servers, no --reload.
- No secrets, no ARG or ENV with credentials; env vars come at run time.
- Do not download and run scripts from the internet. Keep it short.
Return the whole Dockerfile."""

FIX_SYSTEM = """You are Builder, part of Groundcrew. A Docker build in the sandbox failed. Fix \
the Dockerfile so the build passes and the app still starts with its production server on \
0.0.0.0 and the same port.

Rules:
- Read the failing step and the error lines. Fix the cause, not the symptom (for example, a \
Python package that compiles C code needs gcc and the library's dev headers).
- Change as little as possible; keep everything that worked.
- Do not repeat a fix that already failed (see earlier attempts).
- No secrets, no ARG or ENV with credentials. Do not download and run scripts from the internet.
Return the whole corrected Dockerfile, the lines you changed, and the kind of fix."""


def spec_text(spec: AppSpec, port: int) -> str:
    env = ", ".join(
        f"{v.name}{' (secret)' if v.secret else ''}{' (required)' if v.required else ''}"
        for v in spec.env_vars
    )
    rows = [
        ("language", spec.language),
        ("framework", spec.framework),
        ("runtime version", spec.runtime_version),
        ("package manager", spec.package_manager),
        ("install", spec.install_cmd),
        ("build", spec.build_cmd),
        ("start", spec.start_cmd),
        ("port", port),
        ("health path", spec.health_path),
        ("database", spec.database),
        ("env vars", env or None),
    ]
    return "\n".join(f"- {name}: {value}" for name, value in rows if value not in (None, ""))


def files_text(root: Path) -> str:
    view = RepoView(root)
    files = view.files
    listed = "\n".join(files[:MAX_FILES])
    more = f"\n... and {len(files) - MAX_FILES} more" if len(files) > MAX_FILES else ""
    excerpts = []
    for name in BUILD_FILES:
        if name in files and (text := view.text(name)) is not None:
            excerpts.append(f"--- {name}\n{text[:EXCERPT_CHARS]}")
    return f"Files:\n{listed}{more}\n\n" + "\n\n".join(excerpts)


def write_messages(root: Path, spec: AppSpec, port: int) -> list[dict[str, str]]:
    user = f"App:\n{spec_text(spec, port)}\n\n{files_text(root)}"
    return [{"role": "system", "content": WRITE_SYSTEM}, {"role": "user", "content": user}]


def fix_messages(
    root: Path,
    spec: AppSpec,
    port: int,
    dockerfile: str,
    failing_step: str | None,
    error_lines: list[str],
    log_tail: str,
    earlier: list[str],
) -> list[dict[str, str]]:
    numbered = "\n".join(f"{i:>3} {line}" for i, line in enumerate(dockerfile.splitlines(), 1))
    history = "\n".join(earlier) or "none"
    user = (
        f"App:\n{spec_text(spec, port)}\n\n"
        f"Dockerfile that failed:\n{numbered}\n\n"
        f"Failing step: {failing_step or 'unknown'}\n"
        f"Error lines:\n" + "\n".join(error_lines[:12]) + "\n\n"
        f"End of the build log:\n{log_tail[-LOG_CHARS:]}\n\n"
        f"Earlier attempts:\n{history}\n\n{files_text(root)}"
    )
    return [{"role": "system", "content": FIX_SYSTEM}, {"role": "user", "content": user}]
