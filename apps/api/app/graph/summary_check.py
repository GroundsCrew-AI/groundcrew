"""Check Scout's system summary against the graph, bullet by bullet.

Scout writes the summary from the digest, but a small model can still invent links (the app
"stores state" in a Terraform state bucket, two services "use the same image"). We do not
edit Scout's prompt (agents/ is owned by Phase 4); instead every bullet that claims
something the graph contradicts is dropped, and the caller records what was dropped.

A bullet is dropped when it:
1. names a tooling resource (app.graph.roles) that no edge connects to the app;
2. mentions a kind of resource the app does not have (S3, DynamoDB, SQS, Lambda, ...);
3. says services share an image while they pull from different image repositories;
4. names two or more resources and one of them has no edge to the app at all and is related
   to none of the others (not even as a service in a named cluster).
Lines that are not bullets (headings, blank lines) are kept.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.discovery.types import DiscoveredResource
from app.graph.edges import EdgeKind, InferredEdge, key
from app.graph.roles import role

_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
# Words that claim a kind of resource -> the types that would back the claim.
_TYPE_WORDS: dict[str, tuple[str, ...]] = {
    r"\bs3\b|\bbuckets?\b": ("s3:bucket",),
    r"\bdynamo(db)?\b": ("dynamodb:table",),
    r"\bsqs\b|\bqueues?\b": ("sqs:queue",),
    r"\bsns\b|\btopics?\b": ("sns:topic",),
    r"\blambdas?\b": ("lambda:function",),
    r"\bredis\b|\belasticache\b|\bmemcached\b": (
        "elasticache:cluster",
        "elasticache:replicationgroup",
    ),
    r"\bkafka\b|\bmsk\b|\bkinesis\b": ("kafka:cluster", "kinesis:stream"),
}
_SAME_IMAGE = re.compile(r"\bsame\s+(?:container\s+|docker\s+)?image\b", re.IGNORECASE)
_MIN_PREFIX = 12  # "groundcrew-checkout-tfstate-..." still names the bucket
_TRUNCATED = re.compile(r"([\w/-]{" + str(_MIN_PREFIX) + r",})(?:…|\.\.\.)")


@dataclass
class SummaryCheck:
    text: str  # the summary with unsupported bullets removed
    dropped: list[tuple[str, str]] = field(default_factory=list)  # (bullet, why)


def _clean(line: str) -> str:
    return re.sub(r"[`*_]", "", line)


def _mentions(text: str, name: str) -> bool:
    low, n = text.lower(), name.lower()
    if re.search(rf"(?<![\w/-]){re.escape(n)}(?![\w-])", low):
        return True
    # A truncated name: the model shortened it with an ellipsis.
    if len(n) > _MIN_PREFIX + 4:
        for m in _TRUNCATED.finditer(low):
            if n.startswith(m.group(1)):
                return True
    return False


class _Graph:
    def __init__(self, resources: Sequence[DiscoveredResource], edges: Sequence[InferredEdge]):
        self.resources = list(resources)
        self.by_key = {key(r): r for r in resources}
        self.pairs: set[frozenset[str]] = set()
        self.app_linked: set[str] = set()
        for e in edges:
            self.pairs.add(frozenset((e.source, e.target)))
            if e.kind != EdgeKind.defined_in:
                self.app_linked |= {e.source, e.target}
        clusters = {r.external_id: key(r) for r in resources if r.type == "ecs:cluster"}
        for r in resources:
            cluster = clusters.get(str(r.config.get("cluster_arn")))
            if r.type == "ecs:service" and cluster:
                self.pairs.add(frozenset((key(r), cluster)))
        # Names longest first, so "checkout-api" inside "/ecs/checkout-api" is not a match.
        self.names = sorted({r.name for r in resources if len(r.name) >= 3}, key=len, reverse=True)
        self.images = {
            str(c.get("image_repository"))
            for r in resources
            if r.type == "ecs:service"
            for c in r.config.get("containers") or []
            if c.get("image_repository")
        }

    def named(self, text: str) -> list[str]:
        """Names the text mentions, without names that are part of a longer mentioned one."""
        found: list[str] = []
        for name in self.names:
            if _mentions(text, name) and not any(name.lower() in f.lower() for f in found):
                found.append(name)
        return found

    def keys(self, name: str) -> list[str]:
        return [k for k, r in self.by_key.items() if r.name == name]

    def related(self, a: str, b: str) -> bool:
        return any(frozenset((ka, kb)) in self.pairs for ka in self.keys(a) for kb in self.keys(b))

    def problem(self, bullet: str) -> str | None:
        text = _clean(bullet)
        names = self.named(text)
        for name in names:
            for k in self.keys(name):
                if role(self.by_key[k]) == "tooling" and k not in self.app_linked:
                    return f"{name} is tooling, and no edge connects it to the app"
        for pattern, types in _TYPE_WORDS.items():
            if re.search(pattern, text, re.IGNORECASE) and not any(
                r.type in types and role(r) == "app" for r in self.resources
            ):
                return f"the graph has no {types[0]} the app uses"
        if _SAME_IMAGE.search(text) and len(self.images) > 1:
            return f"the services pull different images ({', '.join(sorted(self.images))})"
        if len(names) >= 2:
            for name in names:
                connected = any(k in self.app_linked for k in self.keys(name))
                if not connected and not any(
                    self.related(name, other) for other in names if other != name
                ):
                    return f"no edge relates {name} to {', '.join(n for n in names if n != name)}"
        return None


def check_summary(
    text: str, resources: Sequence[DiscoveredResource], edges: Sequence[InferredEdge]
) -> SummaryCheck:
    graph = _Graph(resources, edges)
    kept: list[str] = []
    dropped: list[tuple[str, str]] = []
    for line in text.splitlines():
        why = graph.problem(line) if _BULLET.match(line) else None
        if why:
            dropped.append((_BULLET.sub("", line).strip(), why))
        else:
            kept.append(line)
    return SummaryCheck(text="\n".join(kept).strip(), dropped=dropped)
