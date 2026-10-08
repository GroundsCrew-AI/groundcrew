"""Scout's system summary (task summarize_system: Nano, reasoning off).

Takes a compact digest of the graph, not raw JSON: one line per resource with its relations
(app.graph.digest builds it from the resources and edges tables), plus risks the graph
module found with rules (public database, security group open to the world, api without a
load balancer or stable endpoint). See docs/phase3-interfaces.md.
"""

import json
from collections.abc import Sequence

from pydantic import BaseModel

from app.llm.budget import RunBudget
from app.llm.client import ChatModel, OnCall
from app.llm.router import Task

MAX_RESOURCES = 150
CHARS_PER_TOKEN = 4
PROMPT_TOKEN_CAP = 6_000

SYSTEM_PROMPT = """You are Scout, part of Groundcrew. Summarize a customer's system for an \
engineer who has never seen it: the main services and where they run, the data stores, the \
deploy pipelines, links across clouds and platforms, and anything risky or unusual. Plain \
English, at most 8 short bullet points; put risks last, starting with "Risk:". Use only the \
resources and risks given; do not invent any."""


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
    risks: Sequence[str] = (),
    on_call: OnCall | None = None,
) -> str:
    if not resources:
        return "No resources found yet."
    ordered = sorted(resources, key=lambda r: (r.provider, r.type, r.name))
    lines: list[str] = []
    used = 0
    budget_chars = (
        PROMPT_TOKEN_CAP * CHARS_PER_TOKEN - len(SYSTEM_PROMPT) - sum(len(r) + 1 for r in risks)
    )
    for resource in ordered[:MAX_RESOURCES]:
        line = json.dumps(resource.model_dump(exclude_none=True, exclude_defaults=True))
        if used + len(line) + 1 > budget_chars:
            break
        lines.append(line)
        used += len(line) + 1
    shown = len(lines)
    if shown < len(ordered):
        lines.append(f"[{len(ordered) - shown} more resources not shown]")
    content = "Resources (one JSON object per line):\n" + "\n".join(lines)
    if risks:
        content += "\n\nRisks found by rules (one per line):\n" + "\n".join(risks)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]
    result = await llm.chat(Task.summarize_system, messages, budget=budget, on_call=on_call)
    return (result.content or "").strip()
