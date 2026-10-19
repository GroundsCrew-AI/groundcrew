"""Kubernetes: an uploaded kubeconfig (stored encrypted) for a service account.

`LiveKubeReader` reads a cluster with the official client, loaded from the kubeconfig text in
memory (nothing written to disk). Tests use `FixtureKubeReader`. The service account is either
the read-only one in infra/k8s-readonly-rbac.yaml (get/list/watch the kinds below and pod logs,
no Secrets) or the kind deployer in infra/kind/rbac.yaml (`view` everywhere, no Secrets);
discovery only ever lists the kinds below with either.

Discovery reads namespaces, Deployments, Services, Ingresses, StatefulSets, and Pods. Secret
objects are never listed. Pod and template env values are dropped except endpoint-like names
(same rule as AWS task definitions); secretKeyRef / envFrom keep only the Secret's name.

Incidents (app/incidents/k8s_rules.py, app/telemetry/k8s.py) also read ReplicaSets (a
Deployment's revisions), ControllerRevisions (a StatefulSet's), Warning events, pod status
(container states, restarts, readiness; never the pod spec's env), and pod logs. ConfigMaps
and Secrets are read as metadata only (`list_metadata`: a PartialObjectMetadataList, asked for
with the Accept header METADATA_ACCEPT), so no value in them ever reaches Groundcrew.
`LiveKubeOperator` adds the one write Medic's approved emergency fixes make: patching a
Deployment's or StatefulSet's pod template (rollout undo, restart).
"""

import asyncio
import builtins
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
    {
        "namespaces",
        "deployments",
        "services",
        "ingresses",
        "statefulsets",
        "pods",
        "replicasets",
        "controllerrevisions",
        "events",
    }
)
# Kinds read only as metadata (`list_metadata`), never with `list`: their data holds values.
K8S_METADATA_KINDS: frozenset[str] = frozenset({"configmaps", "secrets"})
# Asks the API server for metadata only (names, resourceVersion, managedFields): the response
# is a PartialObjectMetadataList, which has no `data` at all.
METADATA_ACCEPT = "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"


class KubeReadRefusedError(ConnectorError):
    pass


def check_kind(kind: str) -> str:
    if kind not in K8S_READ_KINDS:
        raise KubeReadRefusedError(f"Groundcrew does not list {kind} in Kubernetes.")
    return kind


def check_metadata_kind(kind: str) -> str:
    if kind not in K8S_METADATA_KINDS:
        raise KubeReadRefusedError(f"Groundcrew does not read {kind} metadata in Kubernetes.")
    return kind


def metadata_shape(item: dict[str, Any]) -> dict[str, Any]:
    """What Groundcrew keeps of a ConfigMap or Secret: name, namespace, resourceVersion,
    creation time, and when (and by which manager) each managed field set was written.
    Everything else (data, stringData, annotations that may embed values) is dropped."""
    meta = item.get("metadata") or {}
    return {
        "metadata": {
            "name": meta.get("name"),
            "namespace": meta.get("namespace"),
            "resourceVersion": meta.get("resourceVersion"),
            "creationTimestamp": meta.get("creationTimestamp"),
            "managedFields": [
                {k: f.get(k) for k in ("manager", "operation", "time")}
                for f in meta.get("managedFields") or []
            ],
        }
    }


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


# The same check for other hosts Groundcrew reaches for a cluster (a deploy registry, an app
# domain, a verify URL; app.targets.k8s_deploy).
is_private_host = _private


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
    async def list(
        self,
        kind: str,
        namespace: str | None = None,
        *,
        label_selector: str | None = None,
        field_selector: str | None = None,
    ) -> list[dict[str, Any]]: ...

    async def pod_logs(
        self, namespace: str, pod: str, *, since_seconds: int, tail_lines: int
    ) -> str:
        """Every container's log lines, each prefixed with its RFC 3339 timestamp."""
        ...

    async def list_metadata(self, kind: str, namespace: str) -> "builtins.list[dict[str, Any]]":
        """ConfigMaps or Secrets in a namespace, metadata only (`metadata_shape`), never
        their data."""
        ...


class KubeOperator(KubeReader, Protocol):
    async def patch_deployment(
        self, namespace: str, name: str, patch: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """A JSON patch on a Deployment; returns the Deployment after it."""
        ...

    async def patch_statefulset(
        self, namespace: str, name: str, patch: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """A JSON patch on a StatefulSet; returns the StatefulSet after it."""
        ...

    async def can_i(self, verb: str, group: str, resource: str, namespace: str | None) -> bool:
        """SelfSubjectAccessReview: may this kubeconfig's identity do it?"""
        ...


def selected(labels: dict[str, Any], selector: str | None) -> bool:
    """Equality label selectors only ("app=web,tier=api"), as the fixtures need."""
    if not selector:
        return True
    for term in selector.split(","):
        key, _, value = term.partition("=")
        if str(labels.get(key.strip())) != value.strip():
            return False
    return True


def _field(item: dict[str, Any], path: str) -> Any:
    value: Any = item
    for part in path.split("."):
        value = (value or {}).get(part) if isinstance(value, dict) else None
    return value


class FixtureKubeReader:
    """`kubectl get <kind> -A -o json` outputs keyed by kind."""

    def __init__(
        self,
        items: dict[str, list[dict[str, Any]]],
        logs: dict[str, str] | None = None,
    ) -> None:
        self.items = items
        # "namespace/pod" -> log text, timestamp-prefixed like `kubectl logs --timestamps`.
        self.logs = logs or {}
        self.calls: list[str] = []
        self.patches: list[tuple[str, str, list[dict[str, Any]]]] = []
        self.allowed: set[tuple[str, str]] = set()

    @classmethod
    def from_file(cls, path: Path) -> "FixtureKubeReader":
        return cls(json.loads(path.read_text()))

    async def list(
        self,
        kind: str,
        namespace: str | None = None,
        *,
        label_selector: str | None = None,
        field_selector: str | None = None,
    ) -> list[dict[str, Any]]:
        check_kind(kind)
        self.calls.append(kind)
        items = self.items.get(kind, [])
        if namespace is not None:
            items = [i for i in items if (i.get("metadata") or {}).get("namespace") == namespace]
        items = [
            i
            for i in items
            if selected((i.get("metadata") or {}).get("labels") or {}, label_selector)
        ]
        for term in (field_selector or "").split(","):
            if term:
                path, _, value = term.partition("=")
                items = [i for i in items if str(_field(i, path)) == value]
        return items

    async def pod_logs(
        self, namespace: str, pod: str, *, since_seconds: int, tail_lines: int
    ) -> str:
        self.calls.append(f"logs {namespace}/{pod}")
        lines = self.logs.get(f"{namespace}/{pod}", "").splitlines()
        return "\n".join(lines[-tail_lines:])

    async def list_metadata(self, kind: str, namespace: str) -> "builtins.list[dict[str, Any]]":
        """The fixture's ConfigMaps or Secrets in `metadata_shape`, as the live reader returns
        them: a fixture item's data never comes back."""
        check_metadata_kind(kind)
        self.calls.append(f"metadata {kind}")
        return [
            metadata_shape(i)
            for i in self.items.get(kind, [])
            if (i.get("metadata") or {}).get("namespace") == namespace
        ]

    async def patch_deployment(
        self, namespace: str, name: str, patch: "builtins.list[dict[str, Any]]"
    ) -> dict[str, Any]:
        return self._patch("deployments", namespace, name, patch)

    async def patch_statefulset(
        self, namespace: str, name: str, patch: "builtins.list[dict[str, Any]]"
    ) -> dict[str, Any]:
        return self._patch("statefulsets", namespace, name, patch)

    def _patch(
        self, kind: str, namespace: str, name: str, patch: "builtins.list[dict[str, Any]]"
    ) -> dict[str, Any]:
        """Applies `replace` / `add` ops to the fixture workload (all the actions use).
        `patches` records every one, Deployments' and StatefulSets' alike."""
        self.patches.append((namespace, name, patch))
        for item in self.items.get(kind, []):
            meta = item.get("metadata") or {}
            if meta.get("namespace") == namespace and meta.get("name") == name:
                for op in patch:
                    parts = [p.replace("~1", "/") for p in op["path"].strip("/").split("/")]
                    target = item
                    for part in parts[:-1]:
                        target = target.setdefault(part, {})
                    target[parts[-1]] = op["value"]
                return item
        raise ConnectorError(f"{kind} {namespace}/{name} not found.")

    async def can_i(self, verb: str, group: str, resource: str, namespace: str | None) -> bool:
        return (verb, resource) in self.allowed


# One page per request, and a wall clock limit per request, so a big or slow cluster cannot
# stall discovery.
PAGE_SIZE = 500
REQUEST_TIMEOUT_SECONDS = 20


def _pod_shape(item: dict[str, Any]) -> dict[str, Any]:
    """Metadata and status only: the spec (env values) is dropped. Status keeps what incidents
    read: phase, start time, conditions, and each container's state, last state, restarts,
    and readiness (termination messages are cut short)."""
    meta = item.get("metadata") or {}
    status = item.get("status") or {}

    def container(c: dict[str, Any]) -> dict[str, Any]:
        def state(s: dict[str, Any] | None) -> dict[str, Any]:
            out: dict[str, Any] = {}
            for key, value in (s or {}).items():
                value = value or {}
                out[key] = {
                    k: (str(v)[:300] if k == "message" else v)
                    for k, v in value.items()
                    if k in ("reason", "message", "exitCode", "startedAt", "finishedAt")
                }
            return out

        return {
            "name": c.get("name"),
            "image": c.get("image"),
            "ready": c.get("ready"),
            "restartCount": c.get("restartCount", 0),
            "state": state(c.get("state")),
            "lastState": state(c.get("lastState")),
        }

    return {
        "metadata": {
            "name": meta.get("name"),
            "namespace": meta.get("namespace"),
            "labels": meta.get("labels") or {},
            "creationTimestamp": meta.get("creationTimestamp"),
            "deletionTimestamp": meta.get("deletionTimestamp"),
            "ownerReferences": [
                {"kind": o.get("kind"), "name": o.get("name")}
                for o in meta.get("ownerReferences") or []
            ],
        },
        "status": {
            "phase": status.get("phase"),
            "startTime": status.get("startTime"),
            "conditions": [
                {k: c.get(k) for k in ("type", "status", "reason", "lastTransitionTime")}
                for c in status.get("conditions") or []
            ],
            "containerStatuses": [container(c) for c in status.get("containerStatuses") or []],
        },
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
            "replicasets": (
                apps.list_namespaced_replica_set,
                apps.list_replica_set_for_all_namespaces,
            ),
            "controllerrevisions": (
                apps.list_namespaced_controller_revision,
                apps.list_controller_revision_for_all_namespaces,
            ),
            "events": (core.list_namespaced_event, core.list_event_for_all_namespaces),
        }
        namespaced, everywhere = table[kind]
        if namespace is None:
            return everywhere
        return lambda **kw: namespaced(namespace, **kw)

    def _list(
        self,
        kind: str,
        namespace: str | None,
        label_selector: str | None = None,
        field_selector: str | None = None,
    ) -> list[dict[str, Any]]:
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
            if label_selector:
                kwargs["label_selector"] = label_selector
            if field_selector:
                kwargs["field_selector"] = field_selector
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

    async def list(
        self,
        kind: str,
        namespace: str | None = None,
        *,
        label_selector: str | None = None,
        field_selector: str | None = None,
    ) -> list[dict[str, Any]]:
        check_kind(kind)
        self.calls.append(kind)
        return await asyncio.to_thread(self._list, kind, namespace, label_selector, field_selector)

    def _list_metadata(self, kind: str, namespace: str) -> "builtins.list[dict[str, Any]]":
        """GET /api/v1/namespaces/<ns>/<kind> with Accept: METADATA_ACCEPT through the raw
        ApiClient (the typed list_namespaced_secret would ask for the data, and get it). A
        response that is not a PartialObjectMetadataList is refused and dropped unread."""
        items: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            query: list[tuple[str, Any]] = [("limit", PAGE_SIZE)]
            if token:
                query.append(("continue", token))
            try:
                page = self._api.call_api(
                    f"/api/v1/namespaces/{{namespace}}/{kind}",
                    "GET",
                    path_params={"namespace": namespace},
                    query_params=query,
                    header_params={"Accept": METADATA_ACCEPT},
                    auth_settings=["BearerToken"],
                    _preload_content=False,
                    _request_timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                raise ConnectorError(_explain(exc, f"{kind} (metadata)", self.server)) from None
            try:
                data = json.loads(page.data)
            except ValueError:
                raise ConnectorError(
                    f"The cluster returned something that is not JSON for {kind}."
                ) from None
            if data.get("kind") != "PartialObjectMetadataList":
                raise ConnectorError(
                    f"The cluster did not return {kind} as metadata only; Groundcrew skipped them."
                )
            items += [metadata_shape(i) for i in data.get("items") or []]
            token = (data.get("metadata") or {}).get("continue")
            if not token or len(items) >= self.max_items:
                return items[: self.max_items]

    async def list_metadata(self, kind: str, namespace: str) -> "builtins.list[dict[str, Any]]":
        check_metadata_kind(kind)
        self.calls.append(f"metadata {kind}")
        return await asyncio.to_thread(self._list_metadata, kind, namespace)

    def _logs(self, namespace: str, pod: str, since_seconds: int, tail_lines: int) -> str:
        try:
            response = self._core.read_namespaced_pod_log(
                pod,
                namespace,
                since_seconds=since_seconds,
                tail_lines=tail_lines,
                timestamps=True,
                _request_timeout=REQUEST_TIMEOUT_SECONDS,
                _preload_content=False,
            )
        except Exception as exc:
            raise ConnectorError(_explain(exc, "pods/log", self.server)) from None
        return str(response.data.decode("utf-8", "replace"))

    async def pod_logs(
        self, namespace: str, pod: str, *, since_seconds: int, tail_lines: int
    ) -> str:
        self.calls.append(f"logs {namespace}/{pod}")
        return await asyncio.to_thread(self._logs, namespace, pod, since_seconds, tail_lines)

    async def verify(self) -> Sequence[str]:
        """Lists namespaces with these credentials: proves the kubeconfig works. Returns the
        namespace names."""
        return sorted(str(n["metadata"]["name"]) for n in await self.list("namespaces"))

    def close(self) -> None:
        self._api.close()


class LiveKubeOperator(LiveKubeReader):
    """The reader plus the writes approved emergency fixes make. Only app.actions.k8s creates
    one, after checking the connection's operate capability."""

    def _patch(
        self, kind: str, namespace: str, name: str, patch: list[dict[str, Any]]
    ) -> dict[str, Any]:
        method = (
            self._apps.patch_namespaced_stateful_set
            if kind == "statefulsets"
            else self._apps.patch_namespaced_deployment
        )
        try:
            response = method(
                name,
                namespace,
                patch,
                _request_timeout=REQUEST_TIMEOUT_SECONDS,
                _preload_content=False,
            )
        except Exception as exc:
            raise ConnectorError(_explain(exc, f"{kind} (patch)", self.server)) from None
        result: dict[str, Any] = json.loads(response.data)
        return result

    async def patch_deployment(
        self, namespace: str, name: str, patch: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._patch, "deployments", namespace, name, patch)

    async def patch_statefulset(
        self, namespace: str, name: str, patch: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._patch, "statefulsets", namespace, name, patch)

    def _review(self, verb: str, group: str, resource: str, namespace: str | None) -> bool:
        from kubernetes import client

        body = client.V1SelfSubjectAccessReview(
            spec=client.V1SelfSubjectAccessReviewSpec(
                resource_attributes=client.V1ResourceAttributes(
                    verb=verb, group=group, resource=resource, namespace=namespace
                )
            )
        )
        try:
            review = client.AuthorizationV1Api(self._api).create_self_subject_access_review(
                body, _request_timeout=REQUEST_TIMEOUT_SECONDS
            )
        except Exception as exc:
            raise ConnectorError(_explain(exc, "access reviews", self.server)) from None
        return bool(review.status and review.status.allowed)

    async def can_i(self, verb: str, group: str, resource: str, namespace: str | None) -> bool:
        return await asyncio.to_thread(self._review, verb, group, resource, namespace)


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


# Tests replace these to avoid a live cluster.
def live_reader(kubeconfig: str, *, max_items: int = 2000) -> LiveKubeReader:
    return LiveKubeReader(kubeconfig, max_items=max_items)


def live_operator(kubeconfig: str) -> LiveKubeOperator:
    return LiveKubeOperator(kubeconfig)
