"""Tool registry. Every tool declares a name, description, Pydantic argument model, and a
policy tag (read, write, or emergency). The policy tag is required.

Tools are only ever run through app.policy.engine, never by calling `fn` directly.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from app.db.models import ToolPolicy


@dataclass(frozen=True)
class ToolContext:
    run_id: uuid.UUID
    workspace_id: uuid.UUID
    # The repository this run works on (repo tools need it). None when there is none.
    repo_root: Path | None = None


ToolFn = Callable[[ToolContext, Any], Awaitable[str]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    policy: ToolPolicy
    fn: ToolFn

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(),
            },
        }


class ToolNotFoundError(KeyError):
    pass


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def tool[A: BaseModel](
        self,
        *,
        name: str,
        description: str,
        args: type[A],
        policy: ToolPolicy,
    ) -> Callable[
        [Callable[[ToolContext, A], Awaitable[str]]], Callable[[ToolContext, A], Awaitable[str]]
    ]:
        if not isinstance(policy, ToolPolicy):
            raise TypeError(f"Tool {name!r} needs a policy tag: read, write, or emergency.")
        if name in self._tools:
            raise ValueError(f"Tool {name!r} is already registered.")

        def register(
            fn: Callable[[ToolContext, A], Awaitable[str]],
        ) -> Callable[[ToolContext, A], Awaitable[str]]:
            self._tools[name] = ToolSpec(
                name=name,
                description=description,
                args_model=args,
                policy=policy,
                fn=cast(ToolFn, fn),
            )
            return fn

        return register

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(name) from None

    def names(self) -> list[str]:
        return sorted(self._tools)


registry = ToolRegistry()
tool = registry.tool
