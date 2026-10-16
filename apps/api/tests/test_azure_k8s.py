"""Azure (Resource Graph, fixtures; live is TODO) and Kubernetes connectors (fixtures, and the
live reader against a fake API server; the real cluster check is tests/test_kind_live.py)."""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

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


async def test_live_azure_reader_is_not_built_yet() -> None:
    with pytest.raises(ConnectorError, match="preview"):
        await LiveResourceGraph("t", "c", "s").query("Resources", ["sub"])  # TODO(azure-live)


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


# --- LiveKubeReader against a fake API server ----------------------------------------------

TOKEN = "live-reader-test-token"  # noqa: S105


class FakeApi:
    """Serves list calls like the Kubernetes API: `pages[path]` is a list of item pages."""

    def __init__(self) -> None:
        self.pages: dict[str, list[list[dict[str, Any]]]] = {}
        self.status: dict[str, int] = {}
        self.requests: list[str] = []
        self.auth: list[str | None] = []


@pytest.fixture
def fake_api() -> Iterator[tuple[FakeApi, str]]:
    api = FakeApi()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlparse(self.path)
            api.requests.append(self.path)
            api.auth.append(self.headers.get("Authorization"))
            code = api.status.get(url.path)
            if code:
                self._send(code, {"kind": "Status", "code": code, "message": "nope"})
                return
            pages = api.pages.get(url.path)
            if pages is None:
                self._send(404, {"kind": "Status", "code": 404})
                return
            page = int((parse_qs(url.query).get("continue") or ["0"])[0])
            more = page + 1 < len(pages)
            meta = {"continue": str(page + 1)} if more else {}
            self._send(200, {"kind": "List", "metadata": meta, "items": pages[page]})

        def _send(self, code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield api, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _kubeconfig(server: str, token: str = TOKEN) -> str:
    return (
        "apiVersion: v1\n"
        f"clusters: [{{name: c, cluster: {{server: '{server}'}}}}]\n"
        "contexts: [{name: x, context: {cluster: c, user: u}}]\n"
        "current-context: x\n"
        f"users: [{{name: u, user: {{token: {token}}}}}]\n"
    )


def _ns(name: str) -> dict[str, Any]:
    return {"metadata": {"name": name}}


async def test_live_reader_pages_and_sends_the_token(fake_api: tuple[FakeApi, str]) -> None:
    api, server = fake_api
    api.pages["/api/v1/namespaces"] = [[_ns("gc-b"), _ns("default")], [_ns("gc-a")]]
    reader = LiveKubeReader(_kubeconfig(server))
    try:
        assert await reader.verify() == ["default", "gc-a", "gc-b"]
    finally:
        reader.close()
    assert len(api.requests) == 2 and "limit=500" in api.requests[0]
    assert api.auth == [f"Bearer {TOKEN}"] * 2


async def test_live_reader_keeps_only_pod_metadata(fake_api: tuple[FakeApi, str]) -> None:
    api, server = fake_api
    pod = {
        "metadata": {
            "name": "api-1",
            "namespace": "gc-shop",
            "ownerReferences": [{"kind": "ReplicaSet", "name": "api-7d9f", "uid": "u"}],
        },
        "spec": {"containers": [{"env": [{"name": "API_TOKEN", "value": "pod-secret-value"}]}]},
        "status": {"phase": "Running"},
    }
    api.pages["/api/v1/pods"] = [[pod]]
    reader = LiveKubeReader(_kubeconfig(server))
    (item,) = await reader.list("pods")
    assert "pod-secret-value" not in json.dumps(item) and "spec" not in item
    assert item["metadata"]["ownerReferences"] == [{"kind": "ReplicaSet", "name": "api-7d9f"}]
    assert item["status"] == {"phase": "Running"}


async def test_live_reader_maps_every_kind_and_feeds_discovery(
    fake_api: tuple[FakeApi, str],
) -> None:
    api, server = fake_api
    fixture = json.loads((FIXTURES / "k8s.json").read_text())
    paths = {
        "namespaces": "/api/v1/namespaces",
        "deployments": "/apis/apps/v1/deployments",
        "statefulsets": "/apis/apps/v1/statefulsets",
        "services": "/api/v1/services",
        "ingresses": "/apis/networking.k8s.io/v1/ingresses",
        "pods": "/api/v1/pods",
    }
    for kind, path in paths.items():
        api.pages[path] = [fixture[kind]]
    result = await discover_kubernetes(LiveKubeReader(_kubeconfig(server)), "live")
    assert {(r.type, r.name) for r in result.resources} >= {
        ("k8s:namespace", "shop"),
        ("k8s:deployment", "api"),
        ("k8s:ingress", "web"),
    }
    assert not any("secrets" in r for r in api.requests)
    names = {key(r): f"{r.type}:{r.name}" for r in result.resources}
    edges = {(names[e.source], names[e.target]) for e in infer_edges(result.resources, []).edges}
    assert ("k8s:ingress:web", "k8s:service:api") in edges
    assert ("k8s:service:api", "k8s:deployment:api") in edges


async def test_live_reader_explains_auth_failures(fake_api: tuple[FakeApi, str]) -> None:
    api, server = fake_api
    api.status["/api/v1/namespaces"] = 401
    with pytest.raises(ConnectorError, match="rejected the kubeconfig's credentials") as err:
        await LiveKubeReader(_kubeconfig(server)).verify()
    assert TOKEN not in str(err.value)
    api.status["/api/v1/namespaces"] = 403
    with pytest.raises(ConnectorError, match="may not list namespaces"):
        await LiveKubeReader(_kubeconfig(server)).verify()


async def test_live_reader_refuses_secrets_and_bad_kubeconfigs() -> None:
    reader = LiveKubeReader(_kubeconfig("http://127.0.0.1:9"))
    with pytest.raises(KubeReadRefusedError):
        await reader.list("secrets")
    with pytest.raises(ConnectorError, match="Could not load the kubeconfig"):
        LiveKubeReader("apiVersion: v1\nclusters: []\n")


async def test_unknown_cluster_host_says_how_to_reach_kind() -> None:
    reader = LiveKubeReader(_kubeconfig("https://groundcrew-control-plane.invalid:6443"))
    with pytest.raises(ConnectorError, match="make dev KIND=1"):
        await reader.verify()


async def test_engine_discovers_k8s_connections_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine's k8s path decrypts the kubeconfig, reads through live_reader, and closes it."""
    from app.config import get_settings
    from app.connectors import kubernetes as kube
    from app.db.models import Connection
    from app.discovery import engine
    from app.security.crypto import encrypt, generate_key

    monkeypatch.setenv("SECRETS_KEY", generate_key())
    get_settings.cache_clear()
    seen: list[str] = []

    class Reader(FixtureKubeReader):
        closed = False

        def close(self) -> None:
            Reader.closed = True

    def factory(kubeconfig: str, *, max_items: int = 2000) -> Reader:
        seen.append(kubeconfig)
        return Reader(json.loads((FIXTURES / "k8s.json").read_text()))

    monkeypatch.setattr(kube, "live_reader", factory)
    conn = Connection(
        kind="k8s",
        name="Kubernetes groundcrew",
        status="connected",
        config={"cluster": "kind-groundcrew"},
        secret_encrypted=encrypt("apiVersion: v1"),
    )

    async def progress(_text: str) -> None:
        return None

    try:
        result = await engine.DEFAULT_CONNECTORS["k8s"](conn, get_settings(), progress)
    finally:
        get_settings.cache_clear()
    assert seen == ["apiVersion: v1"] and Reader.closed
    assert "k8s" not in engine.PREVIEW_KINDS
    assert {r.account for r in result.resources} == {"kind-groundcrew"}
    assert ("k8s:ingress", "web") in {(r.type, r.name) for r in result.resources}


# --- kubeconfig safety -----------------------------------------------------------------------

import pytest as _pytest  # noqa: E402

from app.connectors.kubernetes import UnsafeKubeconfigError, check_kubeconfig  # noqa: E402

_SAFE = (
    "apiVersion: v1\nkind: Config\ncurrent-context: c\n"
    "contexts: [{name: c, context: {cluster: k, user: u}}]\n"
    "clusters: [{name: k, cluster: {server: 'https://8.8.8.8:6443', "
    "certificate-authority-data: Q0E=}}]\n"
    "users: [{name: u, user: {token: abc}}]\n"
)


def test_a_token_kubeconfig_on_a_public_server_is_accepted() -> None:
    assert check_kubeconfig(_SAFE, allow_private=False)["users"][0]["user"] == {"token": "abc"}


@_pytest.mark.parametrize(
    "user",
    [
        "{exec: {command: /bin/sh, args: ['-c', 'id']}}",
        "{auth-provider: {name: gcp}}",
        "{tokenFile: /etc/shadow}",
        "{client-certificate: /root/cert.pem, client-key: /root/key.pem}",
    ],
)
def test_exec_plugins_providers_and_files_are_refused(user: str) -> None:
    text = _SAFE.replace("{token: abc}", user)
    with _pytest.raises(UnsafeKubeconfigError, match="Groundcrew only accepts"):
        check_kubeconfig(text, allow_private=False)


@_pytest.mark.parametrize(
    "server",
    ["https://127.0.0.1:6443", "https://169.254.169.254", "https://10.0.0.5", "https://[::1]:443",
     "http://8.8.8.8"],
)  # fmt: skip
def test_internal_or_plain_http_servers_are_refused(server: str) -> None:
    with _pytest.raises(UnsafeKubeconfigError):
        check_kubeconfig(_SAFE.replace("https://8.8.8.8:6443", server), allow_private=False)
    # Dev (the local kind cluster) may use them.
    check_kubeconfig(_SAFE.replace("https://8.8.8.8:6443", server), allow_private=True)


def test_ca_files_and_proxies_are_refused() -> None:
    for extra in ("certificate-authority: /etc/ca.pem", "proxy-url: 'http://10.0.0.1:3128'"):
        text = _SAFE.replace("certificate-authority-data: Q0E=", extra)
        with _pytest.raises(UnsafeKubeconfigError, match="proxies and file paths"):
            check_kubeconfig(text, allow_private=False)
