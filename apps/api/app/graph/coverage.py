"""Coverage: how much Groundcrew can do with a resource.

- observed: we know it exists and how it is configured.
- diagnosable: we can also read its signals (a linked log group, or built-in CloudWatch /
  Azure Monitor metrics), so Medic can investigate it.
- actionable: a target adapter can act on it (deploy, roll back, restart). Phase 4 targets
  register here through `register_actionable`; nothing is registered yet.
"""

from collections.abc import Iterable

from app.graph.edges import EdgeKind, InferredEdge

OBSERVED = "observed"
DIAGNOSABLE = "diagnosable"
ACTIONABLE = "actionable"

# Types with metrics we can read without any setup.
METRIC_TYPES = frozenset(
    {
        "ecs:service",
        "rds:db",
        "lambda:function",
        "elasticloadbalancing:loadbalancer/app",
        "microsoft.web/sites",
        "microsoft.dbforpostgresql/flexibleservers",
    }
)

_actionable: set[str] = set()


def register_actionable(resource_type: str) -> None:
    """Hook for target adapters (app/targets): call once per resource type the adapter can act
    on, e.g. register_actionable("ecs:service") from the ECS target module."""
    _actionable.add(resource_type)


def actionable_types() -> frozenset[str]:
    return frozenset(_actionable)


def coverage(resource_type: str, resource_key: str, edges: Iterable[InferredEdge]) -> str:
    if resource_type in _actionable:
        return ACTIONABLE
    has_logs = any(e.source == resource_key and e.kind == EdgeKind.logs_to for e in edges)
    if has_logs or resource_type in METRIC_TYPES:
        return DIAGNOSABLE
    return OBSERVED
