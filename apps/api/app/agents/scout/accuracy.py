"""Compare an AppSpec with a fixture's golden expected_appspec.json, field by field."""

import json
from pathlib import Path
from typing import Any

from app.agents.scout.appspec import AppSpec

GOLDEN_FIELDS = (
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
    "has_dockerfile",
    "env_vars",
)


def load_golden(fixture_dir: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((fixture_dir / "expected_appspec.json").read_text())
    return data


def _env_key(spec_vars: list[dict[str, Any]]) -> list[tuple[str, bool, bool]]:
    return sorted((v["name"], bool(v["required"]), bool(v["secret"])) for v in spec_vars)


def field_matches(spec: AppSpec, golden: dict[str, Any]) -> dict[str, bool]:
    """{field: matches} for every golden field."""
    out: dict[str, bool] = {}
    for name in GOLDEN_FIELDS:
        expected = golden.get(name)
        if name == "env_vars":
            actual = [v.model_dump() for v in spec.env_vars]
            out[name] = _env_key(actual) == _env_key(expected or [])
        else:
            out[name] = getattr(spec, name) == expected
    return out
