"""Azure (Resource Graph) and Kubernetes connectors, on fixtures (live readers are TODO)."""

import json

import pytest

from app.connectors.azure import (
    AZURE_READ_ACTIONS,
    FixtureResourceGraph,
    LiveResourceGraph,
    discovery_query,
)
from app.connectors.kubernetes import (
    FixtureKubeReader,
    KubeReadRefusedError,
    LiveKubeReader,
    kubeconfig_summary,
)
from app.discovery.azure import discover_azure
from app.discovery.kubernetes import discover_kubernetes
from app.discovery.types import ConnectorError
from app.graph.edges import EdgeKind, infer_edges, key
from tests.discovery_helpers import FIXTURES

pytestmark = pytest.mark.anyio


async def test_azure_resource_graph_fixture() -> None:
    graph = FixtureResourceGraph.from_file(FIXTURES / "azure.json")
    result = await discover_azure(graph, "sub-123", "rg-groundcrew-checkout")
    by_name = {r.name: r for r in result.resources}
    assert set(by_name) == {"app-checkout-api", "asp-checkout", "acrcheckout"}
    site = by_name["app-checkout-api"]
    assert (
        site.type == "microsoft.web/sites"
        and site.region == "eastus2"
        and site.account == "sub-123"
    )
    assert site.config["defaultHostName"] == "app-checkout-api.azurewebsites.net"
    assert site.config["image"] == "acrcheckout.azurecr.io/checkout-api:cef79e7"
    assert "rg-groundcrew-checkout" in graph.queries[0]


def test_azure_query_and_actions_are_read_only() -> None:
    with pytest.raises(ConnectorError):
        discovery_query("rg' | project secrets")
    assert all(a.endswith("/read") for a in AZURE_READ_ACTIONS)
    assert not any(
        "listKeys" in a or "config/list" in a or "secrets" in a.lower() for a in AZURE_READ_ACTIONS
    )


async def test_live_readers_are_not_built_yet() -> None:
    with pytest.raises(ConnectorError, match="preview"):
        await LiveResourceGraph("t", "c", "s").query("Resources", ["sub"])  # TODO(azure-live)
    with pytest.raises(ConnectorError, match="preview"):
        await LiveKubeReader("kubeconfig").list("pods")  # TODO(k8s-live)


async def test_kubernetes_fixture_discovery() -> None:
    reader = FixtureKubeReader.from_file(FIXTURES / "k8s.json")
    result = await discover_kubernetes(reader, "demo-cluster")
    kinds = {(r.type, r.name) for r in result.resources}
    assert {
        ("k8s:namespace", "shop"),
        ("k8s:deployment", "api"),
        ("k8s:statefulset", "redis"),
        ("k8s:service", "api"),
        ("k8s:ingress", "web"),
    } <= kinds
    assert ("k8s:namespace", "kube-system") not in kinds
    assert "secrets" not in reader.calls
    api = next(r for r in result.resources if r.type == "k8s:deployment")
    (container,) = api.config["containers"]
    assert container["env_values"] == {"REDIS_HOST": "redis.shop.svc"}
    assert container["secret_refs"] == ["api-env", "db-creds"]
    assert api.config["pods"] == 2
    dumped = json.dumps([r.config for r in result.resources])
    assert "plain-token-must-not-leak" not in dumped and "cG9zdGdyZXM6" not in dumped


async def test_kubernetes_never_lists_secrets() -> None:
    reader = FixtureKubeReader.from_file(FIXTURES / "k8s.json")
    for kind in ("secrets", "configmaps", "serviceaccounts"):
        with pytest.raises(KubeReadRefusedError):
            await reader.list(kind)


async def test_kubernetes_routes_are_calls() -> None:
    result = await discover_kubernetes(FixtureKubeReader.from_file(FIXTURES / "k8s.json"), "c")
    names = {key(r): f"{r.type}:{r.name}" for r in result.resources}
    edges = {
        (names[e.source], e.kind, names[e.target]) for e in infer_edges(result.resources, []).edges
    }
    assert ("k8s:service:api", EdgeKind.calls, "k8s:deployment:api") in edges
    assert ("k8s:ingress:web", EdgeKind.calls, "k8s:service:api") in edges
    assert ("k8s:service:redis", EdgeKind.calls, "k8s:statefulset:redis") in edges


def test_kubeconfig_summary_keeps_no_credentials() -> None:
    text = """
apiVersion: v1
clusters:
- name: demo
  cluster: {server: "https://1.2.3.4:6443", certificate-authority-data: Q0VSVA==}
contexts:
- name: reader
  context: {cluster: demo, user: groundcrew-reader, namespace: shop}
current-context: reader
users:
- name: groundcrew-reader
  user: {token: "eyJhbGciOi.super.secret"}
"""
    summary = kubeconfig_summary(text)
    assert summary == {
        "context": "reader",
        "cluster": "demo",
        "server": "https://1.2.3.4:6443",
        "namespace": "shop",
    }
    with pytest.raises(ConnectorError):
        kubeconfig_summary("not: [valid")
