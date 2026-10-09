"""Kubernetes: an uploaded kubeconfig (stored encrypted) for a read-only service account.

Status: fixtures only. `LiveKubeReader` is not implemented (TODO(k8s-live)); tests and
previews use `FixtureKubeReader`. infra/k8s-readonly-rbac.yaml creates the service account
and a ClusterRole that can get/list/watch the kinds below and nothing else (no Secrets).

Discovery reads namespaces, Deployments, Services, Ingresses, StatefulSets, and Pods. Secret
objects are never listed. Pod and template env values are dropped except endpoint-like names
(same rule as AWS task definitions); secretKeyRef / envFrom keep only the Secret's name.
"""

import json
from pathlib import Path
from typing import Any, Protocol

import yaml

from app.discovery.types import ConnectorError

# Kinds discovery may list. Anything else (Secrets above all) is refused.
K8S_READ_KINDS: frozenset[str] = frozenset(
    {"namespaces", "deployments", "services", "ingresses", "statefulsets", "pods"}
)


class KubeReadRefusedError(ConnectorError):
    pass


def check_kind(kind: str) -> str:
    if kind not in K8S_READ_KINDS:
        raise KubeReadRefusedError(f"Groundcrew does not list {kind} in Kubernetes.")
    return kind


def kubeconfig_summary(text: str) -> dict[str, Any]:
    """Non-secret facts from a kubeconfig: current context, cluster name, server URL,
    namespace. Credentials in it are never returned (the whole file is stored encrypted)."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ConnectorError("That is not a valid kubeconfig (YAML).") from None
    if not isinstance(data, dict) or not data.get("clusters") or not data.get("contexts"):
        raise ConnectorError("The kubeconfig needs at least one cluster and one context.")
    current = data.get("current-context") or data["contexts"][0].get("name")
    context = next((c for c in data["contexts"] if c.get("name") == current), data["contexts"][0])
    ctx = context.get("context") or {}
    cluster = next(
        (c for c in data["clusters"] if c.get("name") == ctx.get("cluster")), data["clusters"][0]
    )
    return {
        "context": current,
        "cluster": cluster.get("name"),
        "server": (cluster.get("cluster") or {}).get("server"),
        "namespace": ctx.get("namespace") or "default",
    }


class KubeReader(Protocol):
    async def list(self, kind: str, namespace: str | None = None) -> list[dict[str, Any]]: ...


class FixtureKubeReader:
    """`kubectl get <kind> -A -o json` outputs keyed by kind."""

    def __init__(self, items: dict[str, list[dict[str, Any]]]) -> None:
        self.items = items
        self.calls: list[str] = []

    @classmethod
    def from_file(cls, path: Path) -> "FixtureKubeReader":
        return cls(json.loads(path.read_text()))

    async def list(self, kind: str, namespace: str | None = None) -> list[dict[str, Any]]:
        check_kind(kind)
        self.calls.append(kind)
        items = self.items.get(kind, [])
        if namespace is None:
            return items
        return [i for i in items if (i.get("metadata") or {}).get("namespace") == namespace]


class LiveKubeReader:
    """TODO(k8s-live): the `kubernetes` client loaded from the decrypted kubeconfig (written to
    a 0600 temp file, deleted after), calling list_namespaced_* / list_*_for_all_namespaces for
    K8S_READ_KINDS only. Not built until a cluster is part of the demo."""

    def __init__(self, kubeconfig: str) -> None:
        self._kubeconfig = kubeconfig

    async def list(self, kind: str, namespace: str | None = None) -> list[dict[str, Any]]:
        check_kind(kind)
        raise ConnectorError("Kubernetes discovery is in preview: live reads are not enabled yet.")
