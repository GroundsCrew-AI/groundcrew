"""Scout: repository -> AppSpec. Hybrid on purpose, not LLM-only.

1. Deterministic detectors read the manifests, lockfiles, Dockerfile, Procfile, env templates,
   and source for ports, env vars, and health routes. Every value has file:line evidence.
2. One Nemotron 3 Nano call (task detect_app_spec: reasoning low, fallback Super) sees the
   detector results plus numbered excerpts, capped at about 6k tokens, and fills what is
   unknown, citing a file and line for each value. Strict JSON schema, normalized enums.
3. Merge: a rule value beats the model on conflict (the conflict is recorded). A model value
   that no cited line supports is not applied: the field is listed in `low_confidence` and the
   value in `unverified`. Every field records whether it came from a rule or the model.
"""

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents.scout.appspec import (
    SPEC_FIELDS,
    AppSpec,
    Conflict,
    DraftEnvVar,
    EnvVar,
    Evidence,
    ScoutDraft,
    Unverified,
)
from app.agents.scout.detectors import (
    ENV_TEMPLATES,
    MANIFESTS,
    Detection,
    detect,
    find_subpath,
    is_secret_name,
)
from app.agents.scout.repo_view import RepoView
from app.llm.budget import RunBudget
from app.llm.client import ChatModel, LLMError, OnCall
from app.llm.router import Task
from app.repos.files import RepoFileError, read_file

logger = logging.getLogger("groundcrew.scout")

CHARS_PER_TOKEN = 4
# Nano reads reliably up to about 6k tokens (probe): the whole prompt stays under this.
PROMPT_TOKEN_CAP = 6_000
MAX_LISTED_FILES = 60
EXCERPT_LINES = 60

SYSTEM_PROMPT = """You are Scout, part of Groundcrew. You read a repository and describe how to \
build and run the app in it, so it can be put in a container.

You get: detector results (facts found by fixed rules, each with file:line evidence), a file \
list, and numbered excerpts of the most relevant files.

Fill every field you can. Rules:
- Use only what the files show. If something is not shown, use null. Do not guess.
- Keep detector values unless an excerpt clearly shows they are wrong.
- For every value you give, add an evidence item: the field, the file path, the line number \
from the excerpt, and a short reason. Cite the exact line that shows the value.
- Commands must be ones the app can run in a container: bind to 0.0.0.0, no dev servers \
(no "next dev", no "--reload") unless nothing else exists.
- env_vars: every variable the app reads, with required (no default in code) and secret \
(passwords, keys, tokens, connection strings)."""

_TOKEN_SPLIT = re.compile(r"[\s\"'=,]+")
# Words too common in commands to count as support. "0.0.0.0" is a token here, not a bind.
_COMMON_TOKENS = frozenset(
    {"run", "start", "install", "build", "--host", "0.0.0.0", "--port", "--bind", "-p", "app"}  # noqa: S104
)
_FRAMEWORK_WORDS: dict[str, tuple[str, ...]] = {
    "nextjs": ("next",),
    "nestjs": ("@nestjs", "nest"),
    "sveltekit": ("sveltejs/kit", "svelte"),
}
_DATABASE_WORDS: dict[str, tuple[str, ...]] = {
    "postgres": ("postgres", "psycopg", "asyncpg", "pg8000", '"pg"', "lib/pq", "pgx"),
    "mysql": ("mysql",),
    "redis": ("redis",),
}
_LANGUAGE_FILES: dict[str, tuple[str, ...]] = {
    "python": (".py", "requirements.txt", "pyproject.toml", "Pipfile"),
    "typescript": (".ts", ".tsx", "tsconfig.json"),
    "javascript": (".js", ".mjs", ".cjs", ".jsx", "package.json"),
    "go": (".go", "go.mod"),
}
_PM_WORDS: dict[str, tuple[str, ...]] = {
    "npm": ("npm", "package-lock"),
    "pip": ("pip", "requirements"),
    "go": ("go ",),
}


@dataclass
class ScoutResult:
    spec: AppSpec
    detection: Detection
    prompt_tokens_estimate: int = 0
    model_used: bool = False
    notes: list[str] = field(default_factory=list)
    # What the model answered before the merge (to measure the model on its own).
    draft: ScoutDraft | None = None


# --- prompt --------------------------------------------------------------------------------


def _detector_summary(detection: Detection) -> dict[str, Any]:
    values = {
        name: {"value": finding.value, "evidence": finding.evidence.location}
        for name, finding in sorted(detection.values.items())
    }
    env = [
        {
            "name": var.name,
            "required": var.required,
            "secret": var.secret,
            "evidence": ev.location,
        }
        for var, ev in (detection.env_vars[n] for n in sorted(detection.env_vars))
    ]
    return {
        "values": values,
        "env_vars": env,
        "has_dockerfile": detection.has_dockerfile is not None,
    }


def _excerpt_files(view: RepoView, detection: Detection) -> list[str]:
    """The most useful files first: manifests, run files, env templates, then code the
    detectors cited, then likely entry points and the README."""
    wanted: list[str] = []
    for name in (
        *MANIFESTS,
        "Procfile",
        "Dockerfile",
        *ENV_TEMPLATES,
        "next.config.js",
        "next.config.mjs",
        "tsconfig.json",
    ):
        if view.exists(name):
            wanted.append(name)
    for evidence in detection.evidence:
        if evidence.file not in wanted and view.exists(evidence.file):
            wanted.append(evidence.file)
    for name in (
        "main.py",
        "app.py",
        "server.js",
        "index.js",
        "src/index.ts",
        "main.go",
        "app/main.py",
        "README.md",
    ):
        if view.exists(name) and name not in wanted:
            wanted.append(name)
    return wanted


def build_prompt(view: RepoView, detection: Detection) -> list[dict[str, str]]:
    """System and user messages, the user message capped to PROMPT_TOKEN_CAP in total."""
    budget = PROMPT_TOKEN_CAP * CHARS_PER_TOKEN - len(SYSTEM_PROMPT) - 1_500  # schema text
    files = view.files
    listing = "\n".join(files[:MAX_LISTED_FILES])
    if len(files) > MAX_LISTED_FILES:
        listing += f"\n[... {len(files) - MAX_LISTED_FILES} more files]"
    header = (
        "## Detector results\n"
        + json.dumps(_detector_summary(detection), indent=1)
        + "\n\n## Files\n"
        + listing
        + "\n\n## Excerpts (line numbers are real)\n"
    )
    parts = [header]
    used = len(header)
    for rel in _excerpt_files(view, detection):
        try:
            excerpt = read_file(view.root, rel, 1, EXCERPT_LINES)
        except RepoFileError:
            continue
        block = f"\n### {rel}\n{excerpt}\n"
        if used + len(block) > budget:
            room = budget - used
            if room > 400:  # a useful partial excerpt
                parts.append(block[:room].rsplit("\n", 1)[0] + "\n[excerpt cut to fit]\n")
            break
        parts.append(block)
        used += len(block)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "".join(parts)},
    ]


# --- support checks ------------------------------------------------------------------------


def _norm(value: Any) -> str:
    return " ".join(str(value).strip().split()).lower()


def _words(field_name: str, value: Any) -> tuple[str, ...]:
    text = _norm(value)
    if field_name == "framework":
        return _FRAMEWORK_WORDS.get(text, (text,))
    if field_name == "database":
        return _DATABASE_WORDS.get(text, (text,))
    if field_name == "package_manager":
        return _PM_WORDS.get(text, (text,))
    if field_name.endswith("_cmd"):
        tokens = [t for t in _TOKEN_SPLIT.split(text) if len(t) >= 3 and t not in _COMMON_TOKENS]
        return tuple(tokens) or (text,)
    return (text,)


def is_supported(view: RepoView, field_name: str, value: Any, evidence: Sequence[Evidence]) -> bool:
    """Does any cited line in the repository show this value?"""
    if value in (None, "none", "other"):
        return False
    for item in evidence:
        line = view.line(item.file, item.line)
        if line is None:
            continue  # the cited file or line does not exist (or may not be read)
        if field_name == "language" and item.file.endswith(_LANGUAGE_FILES.get(_norm(value), ())):
            return True
        lower = line.lower()
        if any(word.lower() in lower for word in _words(field_name, value)):
            return True
    return False


# --- merge ---------------------------------------------------------------------------------


def _same(field_name: str, a: Any, b: Any) -> bool:
    if field_name == "port":
        return str(a) == str(b)
    return _norm(a) == _norm(b)


def merge(view: RepoView, detection: Detection, draft: ScoutDraft | None) -> AppSpec:
    values: dict[str, Any] = {}
    sources: dict[str, Any] = {}
    evidence: list[Evidence] = list(detection.evidence)
    conflicts: list[Conflict] = []
    low: list[str] = []
    unverified: list[Unverified] = []
    model_evidence: dict[str, list[Evidence]] = {}
    for item in draft.evidence if draft else []:
        model_evidence.setdefault(item.field, []).append(item)

    for name in SPEC_FIELDS:
        if name == "env_vars":
            continue
        rule = detection.values.get(name)
        model_value = getattr(draft, name) if draft else None
        if rule is not None:
            values[name], sources[name] = rule.value, "rule"
            if model_value is not None and not _same(name, rule.value, model_value):
                conflicts.append(
                    Conflict(field=name, rule_value=str(rule.value), model_value=str(model_value))
                )
            continue
        if model_value is None:
            continue
        cited = model_evidence.get(name, [])
        if is_supported(view, name, model_value, cited):
            values[name], sources[name] = model_value, "model"
            evidence.extend(e for e in cited if view.line(e.file, e.line) is not None)
        else:
            low.append(name)
            unverified.append(Unverified(field=name, value=str(model_value)))

    env_vars: list[EnvVar] = [detection.env_vars[n][0] for n in sorted(detection.env_vars)]
    for n in sorted(detection.env_vars):
        sources[f"env_vars.{n}"] = "rule"
    known = set(detection.env_vars)
    for proposed in sorted(draft.env_vars if draft else [], key=lambda v: v.name):
        name = proposed.name.strip()
        if not name or name in known:
            continue
        known.add(name)
        if not is_supported(view, "env_vars", name, model_evidence.get("env_vars", [])):
            low.append(f"env_vars.{name}")
            unverified.append(Unverified(field=f"env_vars.{name}", value=name))
            continue
        env_vars.append(_env_from_draft(proposed))
        sources[f"env_vars.{name}"] = "model"
    if env_vars:
        sources["env_vars"] = "rule" if detection.env_vars else "model"

    spec = AppSpec(
        **values,
        env_vars=env_vars,
        has_dockerfile=detection.has_dockerfile is not None,
        subpath=detection.subpath,
        evidence=sorted(evidence, key=lambda e: (e.field, e.file, e.line)),
        sources=sources,
        conflicts=conflicts,
        low_confidence=low,
        unverified=unverified,
    )
    spec.confidence = score(spec)
    return spec


def _env_from_draft(proposed: DraftEnvVar) -> EnvVar:
    secret = proposed.secret if proposed.secret is not None else is_secret_name(proposed.name)
    return EnvVar(
        name=proposed.name.strip(),
        required=proposed.required if proposed.required is not None else True,
        secret=secret,
        example=None if secret else proposed.example,
    )


KEY_FIELDS = ("language", "install_cmd", "start_cmd", "port")


def score(spec: AppSpec) -> float:
    """Deterministic confidence: what is known and how well it is supported."""
    value = 1.0
    value -= 0.2 * sum(getattr(spec, f) is None for f in KEY_FIELDS)
    value -= min(0.3, 0.1 * len(spec.low_confidence))
    value -= min(0.2, 0.05 * len(spec.conflicts))
    return round(max(0.0, min(1.0, value)), 2)


# --- entry point ---------------------------------------------------------------------------


async def scout(
    root: Path,
    llm: ChatModel,
    budget: RunBudget,
    *,
    subpath: str | None = None,
    on_call: OnCall | None = None,
    use_model: bool = True,
) -> ScoutResult:
    """Describe the app in `root` (a materialized RepoSource)."""
    notes: list[str] = []
    app_root = root
    chosen = subpath
    if chosen is None:
        chosen, candidates = find_subpath(RepoView(root))
        if chosen is None and len(candidates) > 1:
            notes.append(f"Several apps found ({', '.join(candidates)}); pass a subpath.")
    if chosen:
        app_root = (root / chosen).resolve()
        if not app_root.is_relative_to(root.resolve()) or not app_root.is_dir():
            raise RepoFileError(f"Subpath {chosen!r} is not a directory in the repository.")
    view = RepoView(app_root)
    detection = detect(view)
    detection.subpath = chosen
    notes.extend(detection.notes)

    draft: ScoutDraft | None = None
    messages = build_prompt(view, detection)
    estimate = sum(len(m["content"]) for m in messages) // CHARS_PER_TOKEN
    model_used = False
    if use_model:
        try:
            result = await llm.chat(
                Task.detect_app_spec, messages, budget=budget, schema=ScoutDraft, on_call=on_call
            )
            draft = result.parsed
            model_used = True
        except LLMError as exc:
            # The rules still give a usable spec; the gap shows in confidence and notes.
            logger.warning("scout model call failed", extra={"error": type(exc).__name__})
            notes.append(f"Model call failed, rules only: {exc}")
    spec = merge(view, detection, draft)
    return ScoutResult(spec, detection, estimate, model_used, notes, draft)
