"""Scout's system summary (task summarize_graph: Nano, reasoning off).

Takes a plain list of resources for now. Phase 3's graph module will pass its resources and
edges in this shape (see docs/phase3-interfaces.md).
"""

import json

from pydantic import BaseModel

from app.llm.budget import RunBudget
from app.llm.client import ChatModel, OnCall
from app.llm.router import Task

MAX_RESOURCES = 150
CHARS_PER_TOKEN = 4
PROMPT_TOKEN_CAP = 6_000

SYSTEM_PROMPT = """You are Scout, part of Groundcrew. Summarize a customer's system for an \
engineer who has never seen it: what runs where, what talks to what, and anything that looks \
risky or unusual. Plain English, at most 8 short bullet points. Use only the resources given; \
do not invent any."""


class ResourceSummary(BaseModel):
    provider: str  # aws | azure | gcp | k8s | vercel | vm
    type: str
    name: str
    region: str | None = None
    # Names of resources this one calls, reads, or is deployed by.
    relations: list[str] = []
    notes: str | None = None


async def summarize_system(
    llm: ChatModel,
    budget: RunBudget,
    resources: list[ResourceSummary],
    *,
    on_call: OnCall | None = None,
) -> str:
    if not resources:
        return "No resources found yet."
    ordered = sorted(resources, key=lambda r: (r.provider, r.type, r.name))
    lines: list[str] = []
    used = 0
    budget_chars = PROMPT_TOKEN_CAP * CHARS_PER_TOKEN - len(SYSTEM_PROMPT)
    for resource in ordered[:MAX_RESOURCES]:
        line = json.dumps(resource.model_dump(exclude_none=True, exclude_defaults=True))
        if used + len(line) + 1 > budget_chars:
            break
        lines.append(line)
        used += len(line) + 1
    shown = len(lines)
    if shown < len(ordered):
        lines.append(f"[{len(ordered) - shown} more resources not shown]")
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "Resources (one JSON object per line):\n" + "\n".join(lines)},
    ]
    result = await llm.chat(Task.summarize_graph, messages, budget=budget, on_call=on_call)
    return (result.content or "").strip()
