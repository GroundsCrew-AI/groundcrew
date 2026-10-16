"""Kubernetes: an uploaded kubeconfig (stored encrypted) for a service account.

`LiveKubeReader` reads a cluster with the official client, loaded from the kubeconfig text in
memory (nothing written to disk). Tests use `FixtureKubeReader`. The service account is either
the read-only one in infra/k8s-readonly-rbac.yaml (get/list/watch the kinds below, no Secrets)
or the kind deployer in infra/kind/rbac.yaml (`view` everywhere, no Secrets); discovery only
ever lists the kinds below with either.

Discovery reads namespaces, Deployments, Services, Ingresses, StatefulSets, and Pods. Secret
objects are never listed. Pod and template env values are dropped except endpoint-like names
(same rule as AWS task definitions); secretKeyRef / envFrom keep only the Secret's name.
"""

import asyncio
import ipaddress
import json
import socket
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

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


# What a stored kubeconfig may contain. Credentials must be inline: an exec plugin or auth
# provider would run a command on Groundcrew's servers, and a *-file / path key would read a
# local file. A proxy would send cluster traffic somewhere else.
_USER_KEYS = frozenset({"token", "client-certificate-data", "client-key-data"})
_CLUSTER_KEYS = frozenset(
    {"server", "certificate-authority-data", "tls-server-name", "insecure-skip-tls-verify"}
)


class UnsafeKubeconfigError(ConnectorError):
    """The kubeconfig asks for something Groundcrew never does with a customer's file."""


def _private(host: str) -> bool:
    """True when the host is, or resolves to, a loopback, private, link-local, or reserved
    address (cloud metadata endpoints included). Unresolvable names are left to the client."""
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except (OSError, UnicodeError):
            return False
        addresses = {ipaddress.ip_address(info[4][0]) for info in infos}
    return any(
        a.is_private
        or a.is_loopback
        or a.is_link_local
        or a.is_reserved
        or a.is_multicast
        or a.is_unspecified
        for a in addresses
    )


def check_kubeconfig(data: Any, *, allow_private: bool | None = None) -> dict[str, Any]:
    """Refuse a kubeconfig that could run commands, read files, or reach internal addresses
    from Groundcrew's servers. Returns the parsed config. `allow_private` (default: the
    K8S_ALLOW_PRIVATE_SERVERS setting, on in APP_ENV=dev for the local kind cluster) allows API
    servers on private addresses."""
    if isinstance(data, str):
        try:
            data = yaml.safe_load(data)
        except yaml.YAMLError:
            raise ConnectorError("That is not a valid kubeconfig (YAML).") from None
    if not isinstance(data, dict):
        raise ConnectorError("That is not a valid kubeconfig.")
    if allow_private is None:
        from app.config import get_settings

        settings = get_settings()
        allow_private = (
            settings.k8s_allow_private_servers
            if settings.k8s_allow_private_servers is not None
            else settings.app_env == "dev"
        )
    for entry in data.get("users") or []:
        user = (entry or {}).get("user") or {}
        extra = sorted(set(user) - _USER_KEYS)
        if extra:
            raise UnsafeKubeconfigError(
                f"User {entry.get('name')!r} uses {', '.join(extra)}. Groundcrew only accepts a "
                "service account token or inline client certificate data (no exec plugins, "
                "auth providers, or file paths)."
            )
    for entry in data.get("clusters") or []:
        cluster = (entry or {}).get("cluster") or {}
        extra = sorted(set(cluster) - _CLUSTER_KEYS)
        if extra:
            raise UnsafeKubeconfigError(
                f"Cluster {entry.get('name')!r} uses {', '.join(extra)}. Put the CA inline "
                "(certificate-authority-data); proxies and file paths are not accepted."
            )
        server = str(cluster.get("server") or "")
        parsed = urlparse(server)
        if parsed.scheme not in ("https", "http") or not parsed.hostname:
            raise UnsafeKubeconfigError(f"Cluster {entry.get('name')!r} has no https server URL.")
        if parsed.scheme == "http" and not allow_private:
            raise UnsafeKubeconfigError("The cluster's server must use https.")
        if not allow_private and _private(parsed.hostname):
            raise UnsafeKubeconfigError(
                f"The cluster's server {parsed.hostname} is a private or internal address. "
                "Groundcrew reaches clusters over the internet only."
            )
    return data


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


# One page per request, and a wall clock limit per request, so a big or slow cluster cannot
# stall discovery.
PAGE_SIZE = 500
REQUEST_TIMEOUT_SECONDS = 20


def _pod_shape(item: dict[str, Any]) -> dict[str, Any]:
    """Pods are only counted per owner: keep metadata and phase, drop the spec (env values)."""
    meta = item.get("metadata") or {}
    return {
        "metadata": {
            "name": meta.get("name"),
            "namespace": meta.get("namespace"),
            "labels": meta.get("labels") or {},
            "ownerReferences": [
                {"kind": o.get("kind"), "name": o.get("name")}
                for o in meta.get("ownerReferences") or []
            ],
        },
        "status": {"phase": (item.get("status") or {}).get("phase")},
    }


class LiveKubeReader:
    """Lists K8S_READ_KINDS from a live cluster with the kubeconfig's credentials (token or
    client certificate). Calls are blocking, so each runs in a thread. Items
    come back as plain dicts in the API's own shape (camelCase), like `kubectl get -o json`.
    The kubeconfig is checked first (`check_kubeconfig`): inline credentials only."""

    def __init__(self, kubeconfig: str, *, max_items: int = 2000) -> None:
        from kubernetes import client, config

        try:
            data = check_kubeconfig(kubeconfig)
            configuration = client.Configuration()
            config.load_kube_config_from_dict(data, client_configuration=configuration)
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError(f"Could not load the kubeconfig: {_short(exc)}") from None
        self._api = client.ApiClient(configuration)
        self._core = client.CoreV1Api(self._api)
        self._apps = client.AppsV1Api(self._api)
        self._networking = client.NetworkingV1Api(self._api)
        self.server = configuration.host
        self.max_items = max_items
        self.calls: list[str] = []

    def _method(self, kind: str, namespace: str | None) -> Callable[..., Any]:
        core, apps, net = self._core, self._apps, self._networking
        if kind == "namespaces":
            return core.list_namespace  # type: ignore[no-any-return]
        table: dict[str, tuple[Callable[..., Any], Callable[..., Any]]] = {
            "deployments": (
                apps.list_namespaced_deployment,
                apps.list_deployment_for_all_namespaces,
            ),
            "statefulsets": (
                apps.list_namespaced_stateful_set,
                apps.list_stateful_set_for_all_namespaces,
            ),
            "services": (core.list_namespaced_service, core.list_service_for_all_namespaces),
            "ingresses": (net.list_namespaced_ingress, net.list_ingress_for_all_namespaces),
            "pods": (core.list_namespaced_pod, core.list_pod_for_all_namespaces),
        }
        namespaced, everywhere = table[kind]
        if namespace is None:
            return everywhere
        return lambda **kw: namespaced(namespace, **kw)

    def _list(self, kind: str, namespace: str | None) -> list[dict[str, Any]]:
        method = self._method(kind, namespace)
        items: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            # Raw JSON, not the client's models: the API's own shape, no model validation.
            kwargs: dict[str, Any] = {
                "limit": PAGE_SIZE,
                "_request_timeout": REQUEST_TIMEOUT_SECONDS,
                "_preload_content": False,
            }
            if token:
                kwargs["_continue"] = token
            try:
                page = method(**kwargs)
            except Exception as exc:
                raise ConnectorError(_explain(exc, kind, self.server)) from None
            try:
                data = json.loads(page.data)
            except ValueError:
                raise ConnectorError(
                    f"The cluster returned something that is not JSON for {kind}."
                ) from None
            batch = data.get("items") or []
            items += [_pod_shape(i) for i in batch] if kind == "pods" else batch
            token = (data.get("metadata") or {}).get("continue")
            if not token or len(items) >= self.max_items:
                return items[: self.max_items]

    async def list(self, kind: str, namespace: str | None = None) -> list[dict[str, Any]]:
        check_kind(kind)
        self.calls.append(kind)
        return await asyncio.to_thread(self._list, kind, namespace)

    async def verify(self) -> Sequence[str]:
        """Lists namespaces with these credentials: proves the kubeconfig works. Returns the
        namespace names."""
        return sorted(str(n["metadata"]["name"]) for n in await self.list("namespaces"))

    def close(self) -> None:
        self._api.close()


def _short(exc: BaseException) -> str:
    return str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__


def _explain(exc: BaseException, kind: str, server: str) -> str:
    """A message a person can act on, never the token or response body."""
    status = getattr(exc, "status", None)
    if status == 401:
        return (
            f"The cluster at {server} rejected the kubeconfig's credentials (401): the token "
            "may be expired or revoked."
        )
    if status == 403:
        return (
            f"The kubeconfig's identity may not list {kind} (403). Check its RBAC (docs/kind.md)."
        )
    text = str(exc)
    dns = (
        "NameResolutionError",
        "Failed to resolve",
        "Name or service not known",
        "name resolution",
    )
    if any(d in text for d in dns):
        return (
            f"Cannot resolve the cluster address {server}. For the local kind cluster, run "
            "Groundcrew with make dev KIND=1 and use .kind/groundcrew-deployer.kubeconfig."
        )
    text = _short(exc)
    if "Connection refused" in text or "timed out" in text.lower() or "Max retries" in text:
        return f"Cannot reach the cluster at {server}: {text}"
    if status:
        return f"The cluster returned {status} while listing {kind}."
    return f"Listing {kind} failed: {text}"


# Tests replace this to avoid a live cluster.
def live_reader(kubeconfig: str, *, max_items: int = 2000) -> LiveKubeReader:
    return LiveKubeReader(kubeconfig, max_items=max_items)
