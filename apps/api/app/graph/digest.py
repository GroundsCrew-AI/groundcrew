"""A compact digest of the graph for Scout's system summary, plus risks found by rules.

The model never sees raw resource JSON: one short line per resource (type, name, region, its
relations by name, a few facts), and the risks below, which are computed, not guessed.
"""

from collections import defaultdict
from collections.abc import Sequence

from app.agents.scout.summary import ResourceSummary
from app.discovery.types import DiscoveredResource
from app.graph.edges import Confidence, EdgeKind, InferredEdge, key

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


def risks(resources: Sequence[DiscoveredResource], edges: Sequence[InferredEdge]) -> list[str]:
    out: list[str] = []
    by_key = {key(r): r for r in resources}
    callers: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        if e.kind == EdgeKind.calls and e.confidence != Confidence.low and e.source in by_key:
            callers[e.target].append(by_key[e.source].name)
    for r in sorted(resources, key=lambda x: (x.type, x.name)):
        c = r.config
        if r.type == "rds:db" and c.get("publicly_accessible"):
            out.append(f"Risk: database {r.name} is publicly accessible.")
        if r.type == "ec2:security-group" and c.get("open_to_world"):
            ports = ", ".join(c["open_to_world"])
            out.append(f"Risk: security group {r.name} allows {ports} from 0.0.0.0/0.")
        if r.type == "ecs:service":
            exposed = any(x.get("ports") for x in c.get("containers") or [])
            public = (c.get("network") or {}).get("assign_public_ip") or c.get("public_ips")
            if exposed and public and not c.get("has_load_balancer"):
                who = callers.get(key(r))
                tail = f" {', '.join(sorted(set(who)))} calls it by that address." if who else ""
                out.append(
                    f"Risk: {r.name} has no load balancer or stable endpoint; it is reached on a "
                    f"task public IP that changes on every deploy.{tail}"
                )
            for d in c.get("deployments") or []:
                if d.get("rollout_state") == "FAILED":
                    out.append(f"Risk: the latest deployment of {r.name} failed.")
                    break
            if (c.get("running_count") or 0) < (c.get("desired_count") or 0):
                out.append(
                    f"Risk: {r.name} runs {c.get('running_count')} of "
                    f"{c.get('desired_count')} tasks."
                )
    return out


def digest(
    resources: Sequence[DiscoveredResource], edges: Sequence[InferredEdge]
) -> list[ResourceSummary]:
    by_key = {key(r): r for r in resources}
    relations: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        if e.kind in _RELATION_KINDS and e.target in by_key and e.confidence != Confidence.low:
            relations[e.source].append(f"{e.kind.value} {by_key[e.target].name}")
    return [
        ResourceSummary(
            provider=r.provider,
            type=r.type,
            name=r.name,
            region=r.region,
            relations=sorted(set(relations.get(key(r), [])))[:8],
            notes=_notes(r),
        )
        for r in resources
        if r.type not in _HIDDEN_TYPES
    ]
