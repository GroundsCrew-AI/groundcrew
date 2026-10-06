"""Fallback parser for tool calls a model wrote as text instead of native tool_calls.

Handles `<tool_call>{...}</tool_call>` tags, ```json fenced blocks, and bare JSON, in the
shapes {"name", "arguments"}, {"tool", "args"}, {"function": {...}}, {"tool_calls": [...]}.
Only names in `allowed` are returned, so stray JSON in an answer is not run as a tool.
"""

import json
import re
import uuid
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

_THINK = re.compile(r"<think>.*?(?:</think>|\Z)", re.DOTALL)
_TAGGED = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|\Z)", re.DOTALL)
_FENCED = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]
    # Set when the model sent arguments that are not a JSON object.
    arguments_error: str | None = None


def strip_reasoning(text: str) -> str:
    """Remove <think> blocks some models put in the content."""
    return _THINK.sub("", text).strip()


def parse_arguments(raw: str | dict[str, Any] | None) -> tuple[dict[str, Any], str | None]:
    if raw is None or raw == "":
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"arguments are not valid JSON: {exc.msg}"
    if not isinstance(value, dict):
        return {}, "arguments must be a JSON object"
    return value, None


def _json_values(text: str) -> Iterable[Any]:
    """Every top-level JSON object or array that appears in the text."""
    decoder = json.JSONDecoder()
    index = 0
    while (start := _next_json_start(text, index)) != -1:
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        yield value
        index = end


def _next_json_start(text: str, index: int) -> int:
    positions = [p for p in (text.find("{", index), text.find("[", index)) if p != -1]
    return min(positions) if positions else -1


def _candidates(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _candidates(item)
    elif isinstance(value, dict):
        if isinstance(value.get("tool_calls"), list):
            yield from _candidates(value["tool_calls"])
        elif isinstance(value.get("function"), dict):
            yield value["function"]
        else:
            yield value


def parse_text_tool_calls(content: str, allowed: Iterable[str]) -> list[ToolCall]:
    allowed_names = set(allowed)
    text = strip_reasoning(content)
    blocks = _TAGGED.findall(text) or _FENCED.findall(text) or [text]

    calls: list[ToolCall] = []
    for block in blocks:
        for value in _json_values(block):
            for obj in _candidates(value):
                name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
                if not isinstance(name, str) or name not in allowed_names:
                    continue
                raw_args = obj.get("arguments", obj.get("args", obj.get("parameters")))
                arguments, error = parse_arguments(raw_args)
                calls.append(
                    ToolCall(
                        id=f"call_text_{uuid.uuid4().hex[:12]}",
                        name=name,
                        arguments=arguments,
                        arguments_error=error,
                    )
                )
    return calls
