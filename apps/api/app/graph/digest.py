"""A compact digest of the graph for Scout's system summary, plus risks found by rules.

The input to Scout's existing `summarize_system(llm, budget, resources)` (task
summarize_graph, Nano): a list of `ResourceSummary`, one per resource, with its relations by
name and a few facts in `notes`. Risks are computed by rules, not guessed, and go into the
`notes` of the resource they are about ("Risk: ..."). The model never sees raw resource JSON,
ARNs, or anything secret.

The digest is deterministic (sorted) and sized to stay under about 6k tokens, Nano's
reliable range: if one line per resource is too big, resources with no relations and no risks
are folded into one line per provider and type, then everything is.
"""

import json
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.scout.summary import ResourceSummary
from app.db.models import Edge, Resource
from app.discovery.types import DiscoveredResource
from app.graph.edges import Confidence, EdgeKind, InferredEdge, key

TOKEN_CAP = 6_000
CHARS_PER_TOKEN = 4
# Room for Scout's system prompt and the "N more resources" line.
_RESERVED_CHARS = 1_000

# Shown through their relations instead of as their own lines.
_HIDDEN_TYPES = frozenset({"ec2:vpc", "ec2:security-group", "iam:role", "logs:log-group"})
_RELATION_KINDS = {
    EdgeKind.calls,
    EdgeKind.reads,
    EdgeKind.uses,
    EdgeKind.for_,
    EdgeKind.pulls_from,
    EdgeKind.logs_to,
    EdgeKind.built_from,
    EdgeKind.deployed_by,
    EdgeKind.routes_to,
    EdgeKind.runs_in,
}


def _notes(r: DiscoveredResource) -> str | None:
    c = r.config
    parts: list[str] = []
    if r.type == "ecs:service":
        parts.append(f"{c.get('running_count')}/{c.get('desired_count')} tasks running")
        ports = sorted(
            {p.get("port") for x in c.get("containers") or [] for p in x.get("ports") or []}
            - {None}
        )
        if ports:
            parts.append("port " + ", ".join(str(p) for p in ports))
        if c.get("public_ips"):
            parts.append("public task IP")
        if not c.get("has_load_balancer"):
            parts.append("no load balancer")
        tags = {x.get("image_tag") for x in c.get("containers") or []} - {None}
        if tags:
            parts.append("image tag " + ", ".join(str(t)[:12] for t in sorted(tags)))
    elif r.type == "rds:db":
        parts.append(f"{c.get('engine')} {c.get('engine_version')}")
        parts.append("public" if c.get("publicly_accessible") else "private")
    elif r.type == "vercel:project":
        if c.get("production_aliases"):
            parts.append("domains " + ", ".join(c["production_aliases"][:2]))
        if c.get("env"):
            parts.append("env " + ", ".join(e["key"] for e in c["env"][:6]))
    elif r.type == "secretsmanager:secret":
        parts.append("metadata only")
        if c.get("last_changed_at"):
            parts.append(f"changed {str(c['last_changed_at'])[:10]}")
    elif r.type == "github:workflow":
        last = c.get("last_run") or {}
        if last:
            parts.append(f"last run {last.get('conclusion') or last.get('status')}")
    return "; ".join(parts) or None


@dataclass(frozen=True)
class Risk:
    resource: str  # key() of the resource it is about
    text: str  # "Risk: ..."


def risks(resources: Sequence[DiscoveredResource], edges: Sequence[InferredEdge]) -> list[str]:
    return [r.text for r in find_risks(resources, edges)]


def find_risks(
    resources: Sequence[DiscoveredResource], edges: Sequence[InferredEdge]
) -> list[Risk]:
    out: list[Risk] = []
    by_key = {key(r): r for r in resources}
    callers: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        if e.kind == EdgeKind.calls and e.confidence != Confidence.low and e.source in by_key:
            callers[e.target].append(by_key[e.source].name)
    for r in sorted(resources, key=lambda x: (x.type, x.name)):
        c = r.config
        if r.type == "rds:db" and c.get("publicly_accessible"):
            out.append(Risk(key(r), f"Risk: database {r.name} is publicly accessible."))
        if r.type == "ec2:security-group" and c.get("open_to_world"):
            ports = ", ".join(c["open_to_world"])
            out.append(
                Risk(key(r), f"Risk: security group {r.name} allows {ports} from 0.0.0.0/0.")
            )
        if r.type == "ecs:service":
            exposed = any(x.get("ports") for x in c.get("containers") or [])
            public = (c.get("network") or {}).get("assign_public_ip") or c.get("public_ips")
            if exposed and public and not c.get("has_load_balancer"):
                who = callers.get(key(r))
                tail = f" {', '.join(sorted(set(who)))} calls it by that address." if who else ""
                text = (
                    f"Risk: {r.name} has no load balancer or stable endpoint; it is reached on a "
                    f"task public IP that changes on every deploy.{tail}"
                )
                out.append(Risk(key(r), text))
            for d in c.get("deployments") or []:
                if d.get("rollout_state") == "FAILED":
                    out.append(Risk(key(r), f"Risk: the latest deployment of {r.name} failed."))
                    break
            if (c.get("running_count") or 0) < (c.get("desired_count") or 0):
                running, desired = c.get("running_count"), c.get("desired_count")
                text = f"Risk: {r.name} runs {running} of {desired} tasks."
                out.append(Risk(key(r), text))
    return out


def _size(items: Sequence[ResourceSummary]) -> int:
    """Characters Scout's prompt spends on these items (one JSON object per line)."""
    return sum(
        len(json.dumps(i.model_dump(exclude_none=True, exclude_defaults=True))) + 1 for i in items
    )


def _folded(provider: str, rtype: str, group: list[ResourceSummary]) -> ResourceSummary:
    names = [g.name for g in group]
    shown = ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
    regions = sorted({g.region for g in group if g.region})
    return ResourceSummary(
        provider=provider,
        type=rtype,
        name=f"{len(group)} x {rtype}",
        region=", ".join(regions[:3]) or None,
        notes=f"grouped: {shown}",
    )


def _fold(items: list[ResourceSummary], *, keep_linked: bool) -> list[ResourceSummary]:
    """One line per provider and type for groups of 2+, keeping linked or risky items."""
    groups: dict[tuple[str, str], list[ResourceSummary]] = defaultdict(list)
    kept: list[ResourceSummary] = []
    for item in items:
        linked = item.relations or (item.notes and "Risk:" in item.notes)
        if keep_linked and linked:
            kept.append(item)
        else:
            groups[(item.provider, item.type)].append(item)
    for (provider, rtype), group in groups.items():
        kept += group if len(group) == 1 else [_folded(provider, rtype, group)]
    return sorted(kept, key=lambda i: (i.provider, i.type, i.name))


def digest(
    resources: Sequence[DiscoveredResource],
    edges: Sequence[InferredEdge],
    *,
    risks: Sequence[Risk] = (),
    token_cap: int = TOKEN_CAP,
) -> list[ResourceSummary]:
    """The workspace graph for Scout: sorted, risks in notes, under `token_cap` tokens when
    folding can get it there (Scout drops whatever still does not fit, and says so)."""
    by_key = {key(r): r for r in resources}
    relations: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        if e.kind in _RELATION_KINDS and e.target in by_key and e.confidence != Confidence.low:
            relations[e.source].append(f"{e.kind.value} {by_key[e.target].name}")
    risk_notes: dict[str, list[str]] = defaultdict(list)
    for risk in risks:
        risk_notes[risk.resource].append(risk.text)
    items = [
        ResourceSummary(
            provider=r.provider,
            type=r.type,
            name=r.name,
            region=r.region,
            relations=sorted(set(relations.get(key(r), [])))[:8],
            notes="; ".join(filter(None, [_notes(r), *risk_notes.get(key(r), [])])) or None,
        )
        for r in resources
        if r.type not in _HIDDEN_TYPES or risk_notes.get(key(r))
    ]
    items.sort(key=lambda i: (i.provider, i.type, i.name))
    budget = token_cap * CHARS_PER_TOKEN - _RESERVED_CHARS
    for keep_linked in (True, False):
        if _size(items) <= budget:
            break
        items = _fold(items, keep_linked=keep_linked)
    return items


def _edge_of(row: Edge, keys: dict[uuid.UUID, str]) -> InferredEdge | None:
    if row.from_id not in keys or row.to_id not in keys:
        return None
    try:
        kind, confidence = EdgeKind(row.kind), Confidence(row.confidence)
    except ValueError:
        return None
    evidence = row.evidence or {}
    return InferredEdge(
        source=keys[row.from_id],
        target=keys[row.to_id],
        kind=kind,
        confidence=confidence,
        rule=str(evidence.get("rule") or ""),
        evidence=str(evidence.get("text") or ""),
    )


async def workspace_digest(
    session: AsyncSession, workspace_id: uuid.UUID, *, token_cap: int = TOKEN_CAP
) -> list[ResourceSummary]:
    """The digest of a workspace's stored graph (non-stale resources and their edges)."""
    from app.discovery.engine import to_discovered

    rows = list(
        await session.scalars(
            select(Resource)
            .where(Resource.workspace_id == workspace_id, Resource.stale.is_(False))
            .order_by(Resource.provider, Resource.type, Resource.name)
        )
    )
    resources = [to_discovered(r) for r in rows]
    keys = {row.id: key(d) for row, d in zip(rows, resources, strict=True)}
    edge_rows = await session.scalars(select(Edge).where(Edge.workspace_id == workspace_id))
    edges = [e for e in (_edge_of(row, keys) for row in edge_rows) if e is not None]
    return digest(resources, edges, risks=find_risks(resources, edges), token_cap=token_cap)
