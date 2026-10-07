"""Schema helpers: strict schema, the prompt instruction, tolerant parsing, value normalizing."""

import enum
from typing import Literal

import pytest
from pydantic import BaseModel

from app.llm.structured import (
    normalize_choice,
    normalize_choices,
    parse_structured,
    strict_schema,
    with_schema_instruction,
)

Language = Literal["javascript", "typescript", "python", "go", "other"]


class Database(enum.StrEnum):
    none = "none"
    postgres = "postgres"
    mysql = "mysql"
    redis = "redis"
    other = "other"


class EnvVar(BaseModel):
    name: str
    kind: Literal["secret", "config", "other"] = "config"


class Spec(BaseModel):
    language: Language
    package_manager: Literal["npm", "yarn", "pnpm", "pip", "poetry", "uv", "other"] | None = None
    database: Database = Database.none
    extra_languages: list[Language] = []
    env_vars: list[EnvVar] = []
    framework: str | None = None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("TypeScript", "typescript"),
        ("  JavaScript ", "javascript"),
        ("javascript", "javascript"),
        ("Rust", "other"),
        ("Go", "go"),
        (3, 3),  # not a string: left for validation
    ],
)
def test_normalize_choice(raw: object, expected: object) -> None:
    assert normalize_choice(raw, ("javascript", "typescript", "go", "other")) == expected


def test_separators_and_no_other_value() -> None:
    assert normalize_choice("Node JS", ("nodejs", "deno")) == "nodejs"
    assert normalize_choice("next.js", ("nextjs", "other")) == "nextjs"
    # No "other" allowed: the value is kept so validation fails and the model is asked to repair.
    assert normalize_choice("Rust", ("python", "go")) == "Rust"


def test_normalize_nested_optional_lists_and_enums() -> None:
    data = {
        "language": "TypeScript",
        "package_manager": "PNPM",
        "database": "PostgreSQL",
        "extra_languages": ["Python", "Elixir"],
        "env_vars": [{"name": "DATABASE_URL", "kind": "Secret"}],
        "framework": "Next.js",  # free text: untouched
    }
    out = normalize_choices(Spec, data)
    assert out == {
        "language": "typescript",
        "package_manager": "pnpm",
        "database": "other",  # "postgresql" is not "postgres": other, never a guess
        "extra_languages": ["python", "other"],
        "env_vars": [{"name": "DATABASE_URL", "kind": "secret"}],
        "framework": "Next.js",
    }
    assert data["language"] == "TypeScript"  # input not mutated
    assert normalize_choices(Spec, {"package_manager": None}) == {"package_manager": None}


def test_parse_structured_normalizes_then_validates() -> None:
    parsed, error = parse_structured('{"language": "Python", "database": "Redis"}', Spec)
    assert error is None
    assert parsed == Spec(language="python", database=Database.redis)


def test_parse_structured_errors_are_short_and_name_the_field() -> None:
    parsed, error = parse_structured('{"language": "python", "env_vars": [{}]}', Spec)
    assert parsed is None and error is not None and "env_vars.0.name" in error
    assert parse_structured("", Spec) == (None, "the reply was empty")
    parsed, error = parse_structured('Sure! {"language": "go"} hope that helps', Spec)
    assert parsed == Spec(language="go")


def test_strict_schema_closes_objects_and_inlines_refs() -> None:
    schema = strict_schema(Spec.model_json_schema())
    assert "$defs" not in str(schema)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(Spec.model_fields)
    item = schema["properties"]["env_vars"]["items"]
    assert item["additionalProperties"] is False and item["required"] == ["name", "kind"]


def test_schema_instruction_merges_into_the_one_system_message() -> None:
    merged = with_schema_instruction(
        [{"role": "system", "content": "You are Scout."}, {"role": "user", "content": "go"}], Spec
    )
    assert [m["role"] for m in merged] == ["system", "user"]
    assert merged[0]["content"].startswith("You are Scout.\n\nReply with one JSON object")
    added = with_schema_instruction([{"role": "user", "content": "go"}], Spec)
    assert [m["role"] for m in added] == ["system", "user"]


def test_choices_without_other() -> None:
    from app.llm.evidence import Findings
    from app.llm.structured import choices_without_other

    assert choices_without_other(Spec) == []
    assert choices_without_other(Findings) == []

    class Closed(BaseModel):
        kind: Literal["a", "b"]
        items: list[Literal["x", "y"]] = []
        nested: EnvVar | None = None

    assert choices_without_other(Closed) == ["kind", "items"]
