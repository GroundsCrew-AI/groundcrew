"""Run the discovery pipeline on the fixtures recorded from the live demo (no AWS, GitHub, or
Vercel calls): inventory, edges, digest, Scout's summary (one real Nano call unless --fake),
and the summary check. Prints edge counts by kind, the summary, and the dropped bullets.

    uv run --project apps/api python scripts/replay_discovery.py [--fake]
"""

import asyncio
import os
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))
os.chdir(REPO_ROOT)  # Settings reads .env from here

from app.agents.scout.summary import summarize_system  # noqa: E402
from app.config import Settings  # noqa: E402
from app.graph.digest import digest, find_risks  # noqa: E402
from app.graph.edges import infer_edges, key  # noqa: E402
from app.graph.roles import role  # noqa: E402
from app.graph.summary_check import check_summary  # noqa: E402
from app.llm.budget import MemoryUsageStore, RunBudget  # noqa: E402
from app.llm.client import TokenFactory  # noqa: E402
from tests.discovery_helpers import demo_inventory  # noqa: E402


async def main(fake: bool) -> None:
    resources, repos = await demo_inventory()
    result = infer_edges(resources, repos)
    edges = result.edges
    print(f"Resources: {len(resources)} ({sum(role(r) == 'tooling' for r in resources)} tooling)")
    print("\nEdges by kind and confidence:")
    counts = Counter((e.kind.value, e.confidence.value) for e in edges)
    for (kind, conf), n in sorted(counts.items()):
        print(f"  {kind:12} {conf:7} {n}")
    print("\ncalls / deployed_by edges:")
    label = {key(r): r.name for r in resources}
    for e in edges:
        if e.kind.value in ("calls", "deployed_by"):
            print(
                f"  {label.get(e.source)} -{e.kind.value}-> {label.get(e.target)} "
                f"[{e.confidence.value}, {e.rule}]"
            )

    items = digest(resources, edges, risks=find_risks(resources, edges))
    settings = Settings()
    if fake:
        text = "\n".join(f"- {i.name}: {', '.join(i.relations) or 'no relations'}" for i in items)
    else:
        budget = RunBudget(usage=MemoryUsageStore(), settings=settings)
        text = await summarize_system(TokenFactory(settings), budget, items)
    checked = check_summary(text, resources, edges)
    print("\nSummary from Scout (before the check):\n" + text)
    print("\nDropped by the check:")
    for bullet, why in checked.dropped or [("(none)", "")]:
        print(f"  - {bullet}" + (f"\n      because {why}" if why else ""))
    print("\nStored summary:\n" + checked.text)


if __name__ == "__main__":
    asyncio.run(main("--fake" in sys.argv))
