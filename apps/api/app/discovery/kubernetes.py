"""Kubernetes discovery through a KubeReader (live or fixtures). Never lists Secrets."""

from typing import Any

from app.connectors.kubernetes import KubeReader
from app.discovery.aws import env_summary
from app.discovery.types import ConnectorResult, DiscoveredResource, Progress, no_progress

PROVIDER = "k8s"
KINDS = {
    "deployments": "k8s:deployment",
    "statefulsets": "k8s:statefulset",
    "services": "k8s:service",
    "ingresses": "k8s:ingress",
}


def external_id(cluster: str, namespace: str | None, kind: str, name: str) -> str:
    return f"k8s:{cluster}:{namespace or '_'}/{kind}/{name}"


def _containers(spec: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for c in spec.get("containers") or []:
        names, values = env_summary(
            {"name": e.get("name"), "value": e.get("value")} for e in c.get("env") or []
        )
        secret_refs = sorted(
            {
                str(((e.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name"))
                for e in c.get("env") or []
                if (e.get("valueFrom") or {}).get("secretKeyRef")
            }
            | {
                str((src.get("secretRef") or {}).get("name"))
                for src in c.get("envFrom") or []
                if src.get("secretRef")
            }
        )
        out.append(
            {
                "name": c.get("name"),
                "image": c.get("image"),
                "ports": [p.get("containerPort") for p in c.get("ports") or []],
                "env_names": names,
                "env_values": values,
                "secret_refs": secret_refs,
            }
        )
    return out


def normalize(kind: str, item: dict[str, Any], cluster: str) -> DiscoveredResource:
    meta = item.get("metadata") or {}
    spec = item.get("spec") or {}
    status = item.get("status") or {}
    ns = meta.get("namespace")
    config: dict[str, Any] = {"namespace": ns, "labels": meta.get("labels") or {}}
    if kind in ("deployments", "statefulsets"):
        template = (spec.get("template") or {}).get("spec") or {}
        config |= {
            "replicas": spec.get("replicas"),
            "ready_replicas": status.get("readyReplicas"),
            "selector": (spec.get("selector") or {}).get("matchLabels") or {},
            "pod_labels": ((spec.get("template") or {}).get("metadata") or {}).get("labels") or {},
            "containers": _containers(template),
        }
    elif kind == "services":
        ingress = (status.get("loadBalancer") or {}).get("ingress") or []
        config |= {
            "service_type": spec.get("type"),
            "cluster_ip": spec.get("clusterIP"),
            "selector": spec.get("selector") or {},
            "ports": [
                {"port": p.get("port"), "target": p.get("targetPort")}
                for p in spec.get("ports") or []
            ],
            "external": [i.get("ip") or i.get("hostname") for i in ingress],
        }
    elif kind == "ingresses":
        rules = spec.get("rules") or []
        config |= {
            "hosts": [r.get("host") for r in rules if r.get("host")],
            "backends": sorted(
                {
                    str(((p.get("backend") or {}).get("service") or {}).get("name"))
                    for r in rules
                    for p in ((r.get("http") or {}).get("paths") or [])
                }
            ),
        }
    return DiscoveredResource(
        provider=PROVIDER,
        type=KINDS[kind],
        external_id=external_id(cluster, ns, kind, str(meta.get("name"))),
        name=str(meta.get("name")),
        account=cluster,
        region=ns,
        tags={str(k): str(v) for k, v in (meta.get("labels") or {}).items()},
        config=config,
    )


async def discover_kubernetes(
    reader: KubeReader,
    cluster: str,
    *,
    progress: Progress = no_progress,
    max_resources: int = 2000,
) -> ConnectorResult:
    namespaces = await reader.list("namespaces")
    resources: list[DiscoveredResource] = [
        DiscoveredResource(
            provider=PROVIDER,
            type="k8s:namespace",
            external_id=external_id(cluster, None, "namespaces", str(n["metadata"]["name"])),
            name=str(n["metadata"]["name"]),
            account=cluster,
            config={
                "labels": (n.get("metadata") or {}).get("labels") or {},
                "phase": (n.get("status") or {}).get("phase"),
            },
        )
        for n in namespaces
        if not str(n["metadata"]["name"]).startswith("kube-")
    ]
    for kind in KINDS:
        for item in await reader.list(kind):
            if str((item.get("metadata") or {}).get("namespace", "")).startswith("kube-"):
                continue
            resources.append(normalize(kind, item, cluster))
    pods = await reader.list("pods")
    by_owner: dict[str, int] = {}
    for pod in pods:
        for owner in (pod.get("metadata") or {}).get("ownerReferences") or []:
            by_owner[str(owner.get("name"))] = by_owner.get(str(owner.get("name")), 0) + 1
    for resource in resources:
        if resource.type in ("k8s:deployment", "k8s:statefulset"):
            # Pods are owned by ReplicaSets named <deployment>-<hash>; count them per workload.
            resource.config["pods"] = sum(
                n
                for owner, n in by_owner.items()
                if owner.startswith(f"{resource.name}-") or owner == resource.name
            )
    await progress(f"Kubernetes {cluster}: {len(resources)} resources")
    return ConnectorResult(
        resources=resources[:max_resources], truncated=len(resources) > max_resources
    )
