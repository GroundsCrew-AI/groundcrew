"""Structured (JSON schema) output: schema for response_format, the prompt instruction, parsing.

Nebius recommends giving the schema both in response_format and in the prompt. Pydantic
validation of the reply is the real guard either way.

The probe (2026-10-07) got valid JSON from all three tiers, but free-text values differed in
casing ("TypeScript", "JavaScript", "javascript"). So every field the code compares on is a
lowercase Literal (or StrEnum) with an "other" value, and normalize_choices() maps the model's
spelling onto it before validation.
TODO(nebius): whether Token Factory enforces `strict: true` (e.g. an enum) is not proven yet;
scripts/probe_followup.py tests it. https://docs.tokenfactory.nebius.com/llms.txt
"""

import enum
import json
import re
import types
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from app.llm.tool_calls import strip_reasoning

Schema = type[BaseModel] | dict[str, Any]

_MAX_DEPTH = 20


def schema_name(schema: Schema) -> str:
    if isinstance(schema, dict):
        return str(schema.get("title") or "output")
    return schema.__name__


def json_schema(schema: Schema) -> dict[str, Any]:
    raw = schema if isinstance(schema, dict) else schema.model_json_schema()
    return strict_schema(raw)


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make a Pydantic JSON schema fit strict mode: inline $defs, every object closed
    (additionalProperties: false) with all properties required. Optional fields stay nullable
    (anyOf with null), so "required" here means "present, maybe null"."""
    defs: dict[str, Any] = schema.get("$defs", {})

    def walk(node: Any, depth: int) -> Any:
        if depth > _MAX_DEPTH:
            raise ValueError("schema nests too deeply (recursive model?)")
        if isinstance(node, list):
            return [walk(item, depth + 1) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            target = defs[node["$ref"].rsplit("/", 1)[-1]]
            merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
            return walk(merged, depth + 1)
        out = {k: walk(v, depth + 1) for k, v in node.items() if k != "$defs"}
        if out.get("type") == "object" and "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"])
        return out

    result: dict[str, Any] = walk(schema, 0)
    return result


def response_format(schema: Schema) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": schema_name(schema), "schema": json_schema(schema), "strict": True},
    }


def with_schema_instruction(messages: list[dict[str, Any]], schema: Schema) -> list[dict[str, Any]]:
    """Add the schema to the system message (one system message only: Nemotron chat templates
    expect a single system message first)."""
    instruction = (
        "Reply with one JSON object only, no prose and no code fences, matching this JSON "
        f"schema:\n{json.dumps(json_schema(schema))}"
    )
    if messages and messages[0].get("role") == "system":
        first = messages[0]
        return [
            {**first, "content": f"{first.get('content') or ''}\n\n{instruction}"},
            *messages[1:],
        ]
    return [{"role": "system", "content": instruction}, *messages]


OTHER = "other"
_SEPARATORS = re.compile(r"[\s_\-./]+")


def _choices(annotation: Any) -> tuple[str, ...] | None:
    """The allowed strings of a Literal[...] or StrEnum annotation, else None."""
    if get_origin(annotation) is Literal:
        options = get_args(annotation)
        return options if all(isinstance(o, str) for o in options) else None
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        values = tuple(m.value for m in annotation)
        return values if all(isinstance(v, str) for v in values) else None
    return None


def _squash(text: str) -> str:
    return _SEPARATORS.sub("", text.strip().lower())


def normalize_choice(value: Any, choices: tuple[str, ...]) -> Any:
    """Map a model's spelling onto an allowed value: case, spaces, and separators are ignored
    ("TypeScript" -> "typescript", "Node JS" -> "nodejs"). Unknown values become "other" when
    it is allowed; otherwise the value is left for validation (and the repair prompt)."""
    if not isinstance(value, str):
        return value
    if value in choices:
        return value
    wanted = _squash(value)
    for choice in choices:
        if _squash(choice) == wanted:
            return choice
    return OTHER if OTHER in choices else value


def _normalize(annotation: Any, value: Any) -> Any:
    choices = _choices(annotation)
    if choices is not None:
        return normalize_choice(value, choices)
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        if value is None:
            return value
        for arg in get_args(annotation):
            if arg is type(None):
                continue
            normalized = _normalize(arg, value)
            if normalized is not value:
                return normalized
        return value
    if origin in (list, tuple, set, frozenset) and isinstance(value, list):
        args = get_args(annotation)
        return [_normalize(args[0], item) for item in value] if args else value
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return normalize_choices(annotation, value)
    return value


def normalize_choices(model: type[BaseModel], data: Any) -> Any:
    """Normalize every Literal / StrEnum field of `model` in `data` (nested models and lists
    included) before validation."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    for name, info in model.model_fields.items():
        key = info.alias or name
        if key in out:
            out[key] = _normalize(info.annotation, out[key])
    return out


def _json_text(content: str) -> str:
    text = strip_reasoning(content).strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


def parse_structured(content: str | None, schema: Schema) -> tuple[Any, str | None]:
    """(parsed value, None) or (None, short error for the repair prompt)."""
    if not content:
        return None, "the reply was empty"
    text = _json_text(content)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        try:
            data, _ = json.JSONDecoder().raw_decode(text, start) if start >= 0 else (None, 0)
        except json.JSONDecodeError as exc:
            return None, f"not valid JSON ({exc.msg} at char {exc.pos})"
        if data is None:
            return None, "no JSON object found"
    if not isinstance(data, dict):
        return None, "expected a JSON object"
    if isinstance(schema, dict):
        return data, None
    try:
        return schema.model_validate(normalize_choices(schema, data)), None
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
            for e in exc.errors(include_url=False)[:6]
        )
        return None, f"does not match the schema: {problems}"
