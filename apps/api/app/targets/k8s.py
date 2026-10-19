"""Kubernetes target: deploy into a gc-<app> namespace with the groundcrew-deployer account.

Apply order (docs/kind.md, docs/phase4-infra.md):

    push image -> Namespace gc-<app> -> RoleBinding to gc-namespace-admin -> Secret
    -> [imagePullSecret] -> Deployment (readiness probe) -> Service -> Ingress <app>.<domain>

Where images go, the Ingress class, host and TLS, and the verify URL come from the
connection's deploy settings (app.targets.k8s_deploy); without them the target deploys to the
local kind cluster exactly as before (GC_KIND_REGISTRY, nginx, <app>.<GC_KIND_APP_DOMAIN>).

The deployer may create namespaces and RoleBindings (never update them), and gets
gc-namespace-admin inside the namespace through the RoleBinding it creates there; the
admission policy keeps every write inside gc-*. Everything else is server-side applied with
field manager "groundcrew". Credentials come from CredentialsProvider.kubeconfig().

Rollback returns the Deployment's pod template to an earlier ReplicaSet's (like `kubectl
rollout undo`); rolling back a first deploy deletes the namespace. Both need an approved
`target_rollback` (emergency) action and never run on their own.
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
import yaml

from app.config import Settings
from app.connectors.credentials import CredentialsProvider
from app.connectors.deploy_infra import k8s_labels, namespace, slug
from app.db.session import SessionFactory, get_session_factory
from app.discovery.types import ConnectorResult
from app.pipeline.events import PreflightCheck, TargetKind, VerifyCheck
from app.policy.actions import (
    TARGET_APPLY,
    TARGET_ROLLBACK,
    ApprovedAction,
    consume_approved_action,
)
from app.sandbox.base import PushTarget, Sandbox
from app.sandbox.registry import kind_target
from app.security.redact import redact
from app.targets.base import (
    MASK,
    ApplyStep,
    Deployment,
    DeploySpec,
    Health,
    LogLine,
    PlanMismatchError,
    TargetError,
    TargetPlan,
    check_spec_matches,
    common_checks,
    env_views,
    secret_values,
)
from app.targets.k8s_deploy import CERT_MANAGER_ISSUER, K8sDeploySettings, is_local_kind

FIELD_MANAGER = "groundcrew"
ROLE_BINDING = "groundcrew-deployer"
NAMESPACE_ADMIN = "gc-namespace-admin"
DEPLOYER_SA = ("groundcrew-system", "groundcrew-deployer")
APP_LABEL = "app.kubernetes.io/name"
MANAGED_LABEL = "app.kubernetes.io/managed-by"
REVISION = "deployment.kubernetes.io/revision"
CPU, MEMORY = "250m", "512Mi"
# Waiting reasons that will not fix themselves; verify stops early on them.
FATAL_WAITING = frozenset(
    {"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "CreateContainerConfigError"}
)
LOG_TAIL_LINES = 50

# (apiVersion, kind) for the dynamic client.
KINDS: dict[str, tuple[str, str]] = {
    "namespaces": ("v1", "Namespace"),
    "rolebindings": ("rbac.authorization.k8s.io/v1", "RoleBinding"),
    "secrets": ("v1", "Secret"),
    "deployments": ("apps/v1", "Deployment"),
    "replicasets": ("apps/v1", "ReplicaSet"),
    "services": ("v1", "Service"),
    "ingresses": ("networking.k8s.io/v1", "Ingress"),
    "pods": ("v1", "Pod"),
}


class KubeApi(Protocol):
    """The few calls the target makes. Objects are plain dicts in the API's own shape."""

    async def get(
        self, kind: str, name: str, namespace: str | None = None
    ) -> dict[str, Any] | None: ...

    async def create(
        self, kind: str, body: dict[str, Any], namespace: str | None = None
    ) -> dict[str, Any]: ...

    async def apply(self, kind: str, body: dict[str, Any], namespace: str) -> dict[str, Any]: ...

    async def merge_patch(
        self, kind: str, name: str, namespace: str, body: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def delete(self, kind: str, name: str, namespace: str | None = None) -> None: ...

    async def list(
        self, kind: str, namespace: str, label_selector: str
    ) -> list[dict[str, Any]]: ...

    async def pod_log(
        self, namespace: str, pod: str, *, since_seconds: int, tail_lines: int
    ) -> str: ...


def unreachable(server: str, exc: BaseException) -> str:
    """Why the cluster could not be reached, with the fix; never a token or response body."""
    text = str(exc)
    if any(
        m in text for m in ("NameResolutionError", "Failed to resolve", "Name or service not known")
    ):
        return (
            f"Cannot resolve the cluster address {server}. For the local kind cluster the API and "
            "worker must be on the kind network: start Groundcrew with make dev KIND=1."
        )
    status = getattr(exc, "status", None)
    if status in (401, 403):
        return f"The cluster at {server} refused the kubeconfig's credentials ({status})."
    first = text.splitlines()[0][:200] if text else type(exc).__name__
    return f"Cannot reach the cluster at {server}: {first}"


class LiveKubeApi:
    """KubeApi over the kubernetes dynamic client. Calls block, so each runs in a thread.
    Errors become TargetError with the status and reason, never a request or response body."""

    def __init__(self, kubeconfig: Path) -> None:
        from kubernetes import config, dynamic

        try:
            api_client = config.new_client_from_config(config_file=str(kubeconfig))
        except Exception as exc:
            raise TargetError(f"Could not load the kubeconfig: {type(exc).__name__}") from None
        self.server = api_client.configuration.host
        try:
            # The client reads the cluster's API list here: the first call to the cluster.
            self._client = dynamic.DynamicClient(api_client)
        except Exception as exc:
            raise TargetError(unreachable(self.server, exc)) from None

    def _resource(self, kind: str) -> Any:
        api_version, name = KINDS[kind]
        return self._client.resources.get(api_version=api_version, kind=name)

    async def _call(self, what: str, fn: Callable[[], Any]) -> Any:
        from kubernetes.dynamic.exceptions import NotFoundError

        try:
            return await asyncio.to_thread(fn)
        except NotFoundError:
            raise
        except Exception as exc:
            status = getattr(exc, "status", None)
            reason = getattr(exc, "reason", None) or type(exc).__name__
            raise TargetError(
                f"{what} failed on {self.server}: {status or ''} {reason}".strip()
            ) from None

    async def get(
        self, kind: str, name: str, namespace: str | None = None
    ) -> dict[str, Any] | None:
        from kubernetes.dynamic.exceptions import NotFoundError

        try:
            obj = await self._call(
                f"get {kind}/{name}",
                lambda: self._resource(kind).get(name=name, namespace=namespace),
            )
        except NotFoundError:
            return None
        return dict(obj.to_dict())

    async def create(
        self, kind: str, body: dict[str, Any], namespace: str | None = None
    ) -> dict[str, Any]:
        name = body["metadata"]["name"]
        obj = await self._call(
            f"create {kind}/{name}",
            lambda: self._resource(kind).create(body=body, namespace=namespace),
        )
        return dict(obj.to_dict())

    async def apply(self, kind: str, body: dict[str, Any], namespace: str) -> dict[str, Any]:
        name = body["metadata"]["name"]
        obj = await self._call(
            f"apply {kind}/{name}",
            lambda: self._resource(kind).server_side_apply(
                body=body,
                name=name,
                namespace=namespace,
                field_manager=FIELD_MANAGER,
                force_conflicts=True,
            ),
        )
        return dict(obj.to_dict())

    async def merge_patch(
        self, kind: str, name: str, namespace: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        obj = await self._call(
            f"patch {kind}/{name}",
            lambda: self._resource(kind).patch(
                body=body,
                name=name,
                namespace=namespace,
                content_type="application/merge-patch+json",
            ),
        )
        return dict(obj.to_dict())

    async def delete(self, kind: str, name: str, namespace: str | None = None) -> None:
        from kubernetes.dynamic.exceptions import NotFoundError

        try:
            await self._call(
                f"delete {kind}/{name}",
                lambda: self._resource(kind).delete(name=name, namespace=namespace),
            )
        except NotFoundError:
            return

    async def list(self, kind: str, namespace: str, label_selector: str) -> list[dict[str, Any]]:
        result = await self._call(
            f"list {kind}",
            lambda: self._resource(kind).get(namespace=namespace, label_selector=label_selector),
        )
        return [dict(item) for item in result.to_dict().get("items") or []]

    async def pod_log(
        self, namespace: str, pod: str, *, since_seconds: int, tail_lines: int
    ) -> str:
        from kubernetes import client

        core = client.CoreV1Api(self._client.client)
        text = await self._call(
            f"logs of {pod}",
            lambda: core.read_namespaced_pod_log(
                pod,
                namespace,
                since_seconds=max(1, since_seconds),
                tail_lines=tail_lines,
                timestamps=True,
            ),
        )
        return str(text or "")


class K8sTarget:
    kind: TargetKind = "k8s"

    def __init__(
        self,
        settings: Settings,
        credentials: CredentialsProvider,
        connection_id: uuid.UUID,
        *,
        cluster: str,
        api: KubeApi | None = None,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        session_factory: SessionFactory | None = None,
        deploy: K8sDeploySettings | None = None,
        server: str | None = None,
    ) -> None:
        self.settings = settings
        self.credentials = credentials
        self.connection_id = connection_id
        self.cluster = cluster
        # The connection's deploy settings; None deploys like the local kind cluster.
        self.deploy = deploy
        # The API server URL from the connection (preflight tells kind from a real cluster).
        self.server = server
        self._api = api
        self._http = http
        self._sleep = sleep
        self._session_factory = session_factory

    # --- plumbing -------------------------------------------------------------------------

    @property
    def session_factory(self) -> SessionFactory:
        """Where approved actions are spent (approvals.used_at)."""
        if self._session_factory is None:
            self._session_factory = get_session_factory()
        return self._session_factory

    @property
    def api(self) -> KubeApi:
        if self._api is None:
            self._api = LiveKubeApi(self.credentials.kubeconfig(self.connection_id))
        return self._api

    def _names(self, app: str) -> dict[str, str]:
        name = slug(app)
        return {
            "app": name,
            "namespace": namespace(self.settings, app),
            "secret": f"{name}-env",
            "pull_secret": f"{name}-registry",
            "tls_secret": f"{name}-tls",
            "host": f"{name}.{self._domain}",
        }

    @property
    def _domain(self) -> str:
        if self.deploy is not None and self.deploy.app_domain:
            return self.deploy.app_domain
        return self.settings.gc_kind_app_domain

    @property
    def _tls(self) -> bool:
        return self.deploy is not None and self.deploy.tls

    def _app_url(self, host: str) -> str:
        return f"{'https' if self._tls else 'http'}://{host}"

    def _image(self, app: str, tag: str) -> str:
        if self.deploy is None:
            return kind_target(self.settings, app, tag).image
        return self.deploy.image(app, tag)

    def _push_target(self, app: str, tag: str) -> PushTarget:
        """kind: GC_KIND_REGISTRY, no login. Otherwise the connection's registry, with its
        login when credentials are saved (decrypted only here)."""
        if self.deploy is None:
            return kind_target(self.settings, app, tag)
        return self.deploy.push_target(app, tag)

    def _ingress_base(self) -> str | None:
        """Where verify sends its requests (always with the app's Host header); None means
        the app's own URL."""
        if self.deploy is not None:
            if self.deploy.public_url_base:
                return self.deploy.public_url_base
            if self.deploy.app_domain:
                return None
        return self.settings.gc_kind_ingress_url.rstrip("/")

    def _labels(self, run_id: uuid.UUID, app: str) -> dict[str, str]:
        return {**k8s_labels(run_id), APP_LABEL: slug(app), MANAGED_LABEL: "groundcrew"}

    # --- discover -------------------------------------------------------------------------

    async def discover(self) -> ConnectorResult:
        """The cluster's gc-* namespaces and what runs in them, with the same external ids as
        Phase 3 discovery, so the graph keeps one node per object."""
        from app.connectors.kubernetes import live_reader
        from app.discovery.kubernetes import discover_kubernetes

        text = self.credentials.kubeconfig(self.connection_id).read_text()
        reader = live_reader(text)
        try:
            found = await discover_kubernetes(reader, self.cluster)
        finally:
            reader.close()
        prefix = self.settings.gc_namespace_prefix
        found.resources = [
            r
            for r in found.resources
            if (r.type == "k8s:namespace" and r.name.startswith(prefix))
            or (r.region or "").startswith(prefix)
        ]
        kept = {r.external_id for r in found.resources}
        found.edges = [
            e for e in found.edges if e.from_external_id in kept and e.to_external_id in kept
        ]
        return found

    # --- plan -----------------------------------------------------------------------------

    def _manifests(
        self,
        spec_like: TargetPlan,
        secrets: dict[str, str] | None,
        pull_secret: str | None = None,
    ) -> list[dict[str, Any]]:
        """The objects apply creates, in order. `secrets` None renders them masked, and so
        does `pull_secret` (the imagePullSecret's .dockerconfigjson) None."""
        n = self._names(spec_like.app)
        labels = self._labels(spec_like.run_id, spec_like.app)
        ns = n["namespace"]
        secret_names = [v.name for v in spec_like.env if v.secret and v.provided]
        container_env: list[dict[str, Any]] = []
        for v in spec_like.env:
            if not v.provided:
                continue
            if v.secret:
                container_env.append(
                    {
                        "name": v.name,
                        "valueFrom": {"secretKeyRef": {"name": n["secret"], "key": v.name}},
                    }
                )
            else:
                container_env.append({"name": v.name, "value": v.value or ""})
        probe: dict[str, Any] = (
            {"httpGet": {"path": spec_like.health_path, "port": spec_like.port}}
            if spec_like.health_path
            else {"tcpSocket": {"port": spec_like.port}}
        )
        probe |= {"initialDelaySeconds": 2, "periodSeconds": 5, "failureThreshold": 3}

        def meta(name: str) -> dict[str, Any]:
            return {"name": name, "namespace": ns, "labels": labels}

        objects: list[dict[str, Any]] = [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": labels}},
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": meta(ROLE_BINDING),
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole",
                    "name": NAMESPACE_ADMIN,
                },
                "subjects": [
                    {"kind": "ServiceAccount", "namespace": DEPLOYER_SA[0], "name": DEPLOYER_SA[1]}
                ],
            },
        ]
        if secret_names:
            data = {name: (MASK if secrets is None else secrets[name]) for name in secret_names}
            objects.append(
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": meta(n["secret"]),
                    "type": "Opaque",
                    "stringData": data,
                }
            )
        pod_extra: dict[str, Any] = {}
        if self.deploy is not None and self.deploy.uses_pull_secret:
            objects.append(
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": meta(n["pull_secret"]),
                    "type": "kubernetes.io/dockerconfigjson",
                    "stringData": {".dockerconfigjson": pull_secret or MASK},
                }
            )
            pod_extra["imagePullSecrets"] = [{"name": n["pull_secret"]}]
        ingress_meta = meta(n["app"])
        ingress_extra: dict[str, Any] = {}
        if self._tls:
            ingress_extra["tls"] = [{"hosts": [n["host"]], "secretName": n["tls_secret"]}]
            issuer = self.deploy.tls_cluster_issuer if self.deploy is not None else None
            if issuer:
                ingress_meta = {**ingress_meta, "annotations": {CERT_MANAGER_ISSUER: issuer}}
        ingress_class = self.deploy.ingress_class if self.deploy is not None else "nginx"
        objects += [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": meta(n["app"]),
                "spec": {
                    "replicas": spec_like.replicas,
                    "revisionHistoryLimit": 10,
                    "selector": {"matchLabels": {APP_LABEL: n["app"]}},
                    "template": {
                        "metadata": {
                            "labels": labels,
                            "annotations": {"groundcrew/run": str(spec_like.run_id)},
                        },
                        "spec": {
                            "containers": [
                                {
                                    "name": n["app"],
                                    "image": spec_like.image,
                                    "ports": [{"containerPort": spec_like.port}],
                                    "env": container_env,
                                    "resources": {
                                        "requests": {
                                            "cpu": spec_like.cpu,
                                            "memory": spec_like.memory,
                                        },
                                        "limits": {"memory": spec_like.memory},
                                    },
                                    "readinessProbe": probe,
                                    "securityContext": {
                                        "allowPrivilegeEscalation": False,
                                        "seccompProfile": {"type": "RuntimeDefault"},
                                    },
                                }
                            ],
                            **pod_extra,
                        },
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": meta(n["app"]),
                "spec": {
                    "selector": {APP_LABEL: n["app"]},
                    "ports": [{"port": 80, "targetPort": spec_like.port}],
                },
            },
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "Ingress",
                "metadata": ingress_meta,
                "spec": {
                    "ingressClassName": ingress_class,
                    **ingress_extra,
                    "rules": [
                        {
                            "host": n["host"],
                            "http": {
                                "paths": [
                                    {
                                        "path": "/",
                                        "pathType": "Prefix",
                                        "backend": {
                                            "service": {"name": n["app"], "port": {"number": 80}}
                                        },
                                    }
                                ]
                            },
                        }
                    ],
                },
            },
        ]
        return objects

    async def plan(self, spec: DeploySpec) -> TargetPlan:
        n = self._names(spec.app)
        image = self._image(n["app"], spec.tag)
        # Inside a namespace that does not exist yet everything is new; the deployer could not
        # look anyway (it reads Secrets only through the RoleBinding apply creates there).
        ns = await self.api.get("namespaces", n["namespace"])
        existing: dict[tuple[str, str], dict[str, Any] | None] = {
            ("namespaces", n["namespace"]): ns
        }
        for kind, name in (
            ("rolebindings", ROLE_BINDING),
            ("secrets", n["secret"]),
            ("secrets", n["pull_secret"]),
            ("deployments", n["app"]),
            ("services", n["app"]),
            ("ingresses", n["app"]),
        ):
            existing[(kind, name)] = await self.api.get(kind, name, n["namespace"]) if ns else None
        deployment = existing[("deployments", n["app"])]
        previous = ((deployment or {}).get("metadata") or {}).get("annotations", {}).get(REVISION)
        plan = TargetPlan(
            target="k8s",
            connection_id=spec.connection_id,
            run_id=spec.run_id,
            app=n["app"],
            image=image,
            source_image_id=spec.source_image_id,
            image_tested=spec.smoke_passed,
            port=spec.port,
            health_path=spec.health_path,
            replicas=spec.replicas,
            cpu=CPU,
            memory=MEMORY,
            env=env_views(spec),
            allow_missing_env=spec.allow_missing_env,
            steps=[],
            rendered="",
            rendered_format="yaml",
            expected_url=self._app_url(n["host"]),
            previous_revision=str(previous) if previous else None,
        )
        objects = self._manifests(plan, None)
        kind_of = {
            "Namespace": "namespaces",
            "RoleBinding": "rolebindings",
            "Secret": "secrets",
            "Deployment": "deployments",
            "Service": "services",
            "Ingress": "ingresses",
        }
        login = f", login as {self.deploy.username}" if self.deploy and self.deploy.username else ""
        steps = [
            ApplyStep(
                kind="image",
                name=image,
                action="push",
                detail=f"{spec.source_image_id[:19]} from the sandbox{login}",
            )
        ]
        for obj in objects:
            kind = kind_of[obj["kind"]]
            found = existing.get((kind, obj["metadata"]["name"])) is not None
            create_only = kind in ("namespaces", "rolebindings")
            probe = plan.health_path or f"tcp/{plan.port}"
            action = ("unchanged" if create_only else "update") if found else "create"
            ingress = f"{plan.expected_url}, class {obj.get('spec', {}).get('ingressClassName')}"
            detail = {
                "rolebindings": f"{NAMESPACE_ADMIN} for {DEPLOYER_SA[1]}",
                "deployments": f"{plan.replicas} replica, {CPU} / {MEMORY}, readiness {probe}",
                "services": f"port 80 -> {plan.port}",
                "ingresses": ingress + (", TLS" if self._tls else ""),
            }.get(kind)
            if obj.get("type") == "kubernetes.io/dockerconfigjson" and self.deploy is not None:
                detail = f"imagePullSecret for {self.deploy.registry_host}"
            steps.append(
                ApplyStep(
                    kind=obj["kind"], name=obj["metadata"]["name"], action=action, detail=detail
                )
            )
        plan.steps = steps
        plan.rendered = yaml.safe_dump_all(objects, sort_keys=False)
        return plan

    async def preflight(self, plan: TargetPlan) -> list[PreflightCheck]:
        """Read-only: the plan's own checks, then whether the names belong to Groundcrew."""
        checks = common_checks(plan)
        checks.append(self._registry_check())
        n = self._names(plan.app)
        ns = await self.api.get("namespaces", n["namespace"])

        def ours(obj: dict[str, Any]) -> bool:
            labels = (obj.get("metadata") or {}).get("labels") or {}
            return bool(labels.get("project") == "groundcrew")

        if ns is not None and (ns.get("status") or {}).get("phase") == "Terminating":
            checks.append(
                PreflightCheck(
                    name="name_collision",
                    status="fail",
                    detail=f"Namespace {n['namespace']} is still being deleted; deploy again "
                    "in a minute",
                )
            )
        elif ns is not None and not ours(ns):
            checks.append(
                PreflightCheck(
                    name="name_collision",
                    status="fail",
                    detail=f"Namespace {n['namespace']} exists and was not created by Groundcrew",
                )
            )
        elif ns is None:
            checks.append(
                PreflightCheck(
                    name="name_collision", status="pass", detail=f"No existing {n['namespace']}"
                )
            )
        else:
            deployment = await self.api.get("deployments", n["app"], n["namespace"])
            if deployment is not None and not ours(deployment):
                checks.append(
                    PreflightCheck(
                        name="name_collision",
                        status="fail",
                        detail=f"Deployment {n['app']} exists and was not created by Groundcrew",
                    )
                )
            else:
                checks.append(
                    PreflightCheck(
                        name="name_collision",
                        status="pass",
                        detail=f"Updates Groundcrew's {n['namespace']}"
                        + (
                            f" (revision {plan.previous_revision})"
                            if plan.previous_revision
                            else ""
                        ),
                    )
                )
        return checks

    def _registry_check(self) -> PreflightCheck:
        """A real cluster cannot pull from the local kind registry: without deploy settings,
        only the local kind cluster may be deployed to."""
        if self.deploy is None:
            if is_local_kind(self.server):
                return PreflightCheck(
                    name="other",
                    status="pass",
                    detail=f"Local kind cluster: images go to {self.settings.gc_kind_registry}",
                )
            return PreflightCheck(
                name="other",
                status="fail",
                detail=f"This cluster ({self.server}) is not the local kind cluster and has no "
                "deploy settings, so it could not pull the image from "
                f"{self.settings.gc_kind_registry}. An owner sets a registry and app domain "
                "under Deploy settings on the Connections page (docs/kind.md, Real clusters).",
            )
        d = self.deploy
        parts = [
            f"Images go to {d.registry}"
            + (f" (login as {d.username})" if d.has_credentials else " (no login)"),
            f"ingress class {d.ingress_class}",
        ]
        if d.uses_pull_secret:
            parts.append("pulled with an imagePullSecret")
        if d.app_domain is None and not is_local_kind(self.server):
            return PreflightCheck(
                name="other",
                status="warn",
                detail=", ".join(parts) + ". No app domain is set, so the app answers at "
                f"<app>.{self.settings.gc_kind_app_domain} and verify may not reach it.",
            )
        return PreflightCheck(name="other", status="pass", detail=", ".join(parts))

    # --- apply ----------------------------------------------------------------------------

    async def _write(self, kind: str, body: dict[str, Any], ns: str) -> dict[str, Any]:
        """Server-side apply. The first writes after a new RoleBinding can be refused for a
        moment while RBAC catches up, so a 403 is retried for a few seconds."""
        for attempt in range(6):
            try:
                return await self.api.apply(kind, body, ns)
            except TargetError as exc:
                if " 403 " not in f" {exc} " or attempt == 5:
                    raise
                await self._sleep(1.0)
        raise AssertionError("unreachable")

    async def apply(
        self, plan: TargetPlan, spec: DeploySpec, token: ApprovedAction, sandbox: Sandbox
    ) -> Deployment:
        await consume_approved_action(
            self.session_factory,
            token,
            run_id=plan.run_id,
            action=TARGET_APPLY,
            plan_hash=plan.plan_hash(),
        )
        check_spec_matches(plan, spec)
        n = self._names(plan.app)
        ns = n["namespace"]
        # The connection's deploy settings may have changed since the plan was approved.
        if self._image(n["app"], spec.tag) != plan.image or (
            yaml.safe_dump_all(self._manifests(plan, None), sort_keys=False) != plan.rendered
        ):
            raise PlanMismatchError(
                "The cluster's deploy settings changed after this plan was approved; plan the "
                "deploy again."
            )
        # 1. Push: the first thing that leaves the sandbox, only now that the plan is approved.
        await sandbox.push(spec.source_image, self._push_target(n["app"], spec.tag))
        # Plain values are in the plan already (check_spec_matches); secrets come from the spec.
        pull = (
            self.deploy.dockerconfigjson()
            if self.deploy is not None and self.deploy.uses_pull_secret
            else None
        )
        rendered = self._manifests(plan, secret_values(spec), pull)
        for obj in rendered:
            kind = {"Namespace": "namespaces", "RoleBinding": "rolebindings"}.get(obj["kind"])
            name = obj["metadata"]["name"]
            if kind == "namespaces":
                if await self.api.get(kind, name) is None:
                    await self.api.create(kind, obj)
            elif kind == "rolebindings":
                if await self.api.get(kind, name, ns) is None:
                    await self.api.create(kind, obj, ns)
            else:
                plural = {
                    "Secret": "secrets",
                    "Deployment": "deployments",
                    "Service": "services",
                    "Ingress": "ingresses",
                }[obj["kind"]]
                await self._write(plural, obj, ns)
        revision = await self._revision(ns, n["app"])
        return Deployment(
            target="k8s",
            connection_id=plan.connection_id,
            run_id=plan.run_id,
            app=plan.app,
            image=plan.image,
            revision=revision,
            previous_revision=plan.previous_revision,
            location=ns,
            live_url=plan.expected_url,
            port=plan.port,
            health_path=plan.health_path,
            external_id=f"k8s:{self.cluster}:{ns}/deployments/{n['app']}",
        )

    async def _revision(self, ns: str, name: str, attempts: int = 10) -> str | None:
        """The Deployment's revision once the controller has seen the new spec."""
        for _ in range(attempts):
            obj = await self.api.get("deployments", name, ns) or {}
            meta, status = obj.get("metadata") or {}, obj.get("status") or {}
            revision = (meta.get("annotations") or {}).get(REVISION)
            if revision and (status.get("observedGeneration") or 0) >= (
                meta.get("generation") or 0
            ):
                return str(revision)
            await self._sleep(1.0)
        return None

    # --- verify and logs ------------------------------------------------------------------

    async def _pods(self, deployment: Deployment) -> list[dict[str, Any]]:
        return await self.api.list(
            "pods", deployment.location, f"{APP_LABEL}={slug(deployment.app)}"
        )

    @staticmethod
    def _failed_condition(deployment: dict[str, Any]) -> str | None:
        """A Deployment that cannot create pods (quota, admission) or gave up progressing."""
        for c in (deployment.get("status") or {}).get("conditions") or []:
            failed = c.get("type") == "ReplicaFailure" and c.get("status") == "True"
            stuck = c.get("type") == "Progressing" and c.get("reason") == "ProgressDeadlineExceeded"
            if failed or stuck:
                return f"{c.get('reason')}: {redact(str(c.get('message') or ''))[:200]}"
        return None

    @staticmethod
    def _fatal(pods: list[dict[str, Any]]) -> str | None:
        for pod in pods:
            for status in (pod.get("status") or {}).get("containerStatuses") or []:
                waiting = (status.get("state") or {}).get("waiting") or {}
                reason = waiting.get("reason")
                if reason in FATAL_WAITING or (
                    reason == "CrashLoopBackOff" and (status.get("restartCount") or 0) >= 2
                ):
                    message = str(waiting.get("message") or "")[:200]
                    return f"{pod['metadata']['name']}: {reason}" + (
                        f" ({redact(message)})" if message else ""
                    )
        return None

    async def verify(self, deployment: Deployment) -> Health:
        started = time.monotonic()
        deadline = started + self.settings.gc_verify_timeout_seconds
        name, ns = slug(deployment.app), deployment.location
        checks: list[VerifyCheck] = []

        rollout: VerifyCheck | None = None
        while rollout is None:
            obj = await self.api.get("deployments", name, ns) or {}
            spec, status, meta = (
                obj.get("spec") or {},
                obj.get("status") or {},
                obj.get("metadata") or {},
            )
            want = spec.get("replicas") or 1
            ready = status.get("availableReplicas") or 0
            if (
                (status.get("observedGeneration") or 0) >= (meta.get("generation") or 0)
                and (status.get("updatedReplicas") or 0) >= want
                and ready >= want
                and not status.get("unavailableReplicas")
            ):
                rollout = VerifyCheck(
                    name="rollout", ok=True, detail=f"{ready}/{want} replicas ready"
                )
                break
            fatal = self._failed_condition(obj) or self._fatal(await self._pods(deployment))
            waited = int(self.settings.gc_verify_timeout_seconds)
            if fatal or time.monotonic() >= deadline:
                rollout = VerifyCheck(
                    name="rollout",
                    ok=False,
                    detail=fatal or f"{ready}/{want} replicas ready after {waited} s",
                )
                break
            await self._sleep(2.0)
        checks.append(rollout)

        if rollout.ok:
            checks += await self._http_checks(deployment, deadline)
        decisive = [c for c in checks if c.name != "root" or not deployment.health_path]
        ok = all(c.ok for c in decisive)
        return Health(
            ok=ok,
            checks=checks,
            duration_ms=int((time.monotonic() - started) * 1000),
            live_url=deployment.live_url if ok else None,
            logs_tail=None if ok else await self._logs_tail(deployment),
        )

    async def _http_checks(self, deployment: Deployment, deadline: float) -> list[VerifyCheck]:
        host = f"{slug(deployment.app)}.{self._domain}"
        app_url = self._app_url(host)
        base = self._ingress_base() or app_url
        client = self._http or httpx.AsyncClient(timeout=5.0)
        out: list[VerifyCheck] = []
        try:
            paths: list[tuple[Literal["health", "root"], str]] = []
            if deployment.health_path:
                paths.append(("health", deployment.health_path))
            paths.append(("root", "/"))
            for name, path in paths:
                last, code, latency = "no answer", None, None
                # The ingress controller takes a few seconds to pick up a new Ingress (404).
                while True:
                    t0 = time.monotonic()
                    try:
                        response = await client.get(f"{base}{path}", headers={"Host": host})
                        code, latency = response.status_code, int((time.monotonic() - t0) * 1000)
                        last = f"HTTP {code}"
                        if code < 400:
                            break
                    except httpx.HTTPError as exc:
                        last = type(exc).__name__
                    if name == "root" or time.monotonic() >= deadline:
                        break
                    await self._sleep(2.0)
                out.append(
                    VerifyCheck(
                        name=name,
                        ok=code is not None and code < 400,
                        detail=last,
                        url=f"{app_url}{path}",
                        status_code=code,
                        latency_ms=latency,
                    )
                )
        finally:
            if self._http is None:
                await client.aclose()
        return out

    async def _logs_tail(self, deployment: Deployment) -> str | None:
        lines = await self.logs(deployment, datetime.now(UTC) - timedelta(minutes=10))
        if not lines:
            return None
        return "\n".join(f"{line.source}: {line.message}" for line in lines[-LOG_TAIL_LINES:])

    async def logs(self, deployment: Deployment, since: datetime) -> list[LogLine]:
        seconds = int((datetime.now(UTC) - since).total_seconds())
        out: list[LogLine] = []
        for pod in await self._pods(deployment):
            pod_name = pod["metadata"]["name"]
            text = await self.api.pod_log(
                deployment.location, pod_name, since_seconds=seconds, tail_lines=500
            )
            for raw in text.splitlines():
                stamp, _, message = raw.partition(" ")
                try:
                    at: datetime | None = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                except ValueError:
                    at, message = None, raw
                out.append(LogLine(at=at, source=pod_name, message=redact(message)))
        out.sort(key=lambda line: line.at or since)
        return out

    # --- rollback -------------------------------------------------------------------------

    async def rollback_plan(self, deployment: Deployment) -> TargetPlan:
        """Back to the previous revision, or delete the namespace of a first deploy."""
        n = self._names(deployment.app)
        to = deployment.previous_revision
        steps = (
            [
                ApplyStep(
                    kind="Deployment",
                    name=n["app"],
                    action="update",
                    detail=f"pod template of revision {to}",
                )
            ]
            if to
            else [
                ApplyStep(
                    kind="Namespace",
                    name=n["namespace"],
                    action="delete",
                    detail="first deploy: removes the Deployment, Service, Ingress, and Secret",
                )
            ]
        )
        return TargetPlan(
            action="rollback",
            target="k8s",
            connection_id=deployment.connection_id,
            run_id=deployment.run_id,
            app=n["app"],
            image=deployment.image,
            source_image_id=None,
            image_tested=True,
            port=deployment.port,
            health_path=deployment.health_path,
            replicas=1,
            cpu=CPU,
            memory=MEMORY,
            env=[],
            allow_missing_env=False,
            steps=steps,
            rendered=yaml.safe_dump(
                {
                    "rollback": {
                        "namespace": n["namespace"],
                        "deployment": n["app"],
                        "from_revision": deployment.revision,
                        "to_revision": to,
                    }
                },
                sort_keys=False,
            ),
            rendered_format="yaml",
            previous_revision=deployment.revision,
            rollback_to_revision=to,
        )

    async def rollback(self, plan: TargetPlan, token: ApprovedAction) -> Deployment:
        await consume_approved_action(
            self.session_factory,
            token,
            run_id=plan.run_id,
            action=TARGET_ROLLBACK,
            plan_hash=plan.plan_hash(),
        )
        n = self._names(plan.app)
        base = {
            "target": "k8s",
            "connection_id": plan.connection_id,
            "run_id": plan.run_id,
            "app": plan.app,
            "location": n["namespace"],
            "port": plan.port,
            "health_path": plan.health_path,
        }
        if plan.rollback_to_revision is None:
            await self.api.delete("namespaces", n["namespace"])
            return Deployment(
                **base,
                image=plan.image,
                revision=None,
                previous_revision=plan.previous_revision,
                status="deleted",
            )
        replicasets = await self.api.list("replicasets", n["namespace"], f"{APP_LABEL}={n['app']}")
        match = next(
            (
                rs
                for rs in replicasets
                if ((rs.get("metadata") or {}).get("annotations") or {}).get(REVISION)
                == plan.rollback_to_revision
            ),
            None,
        )
        if match is None:
            raise TargetError(
                f"Revision {plan.rollback_to_revision} of {n['app']} is no longer in the cluster."
            )
        template = match["spec"]["template"]
        labels = dict((template.get("metadata") or {}).get("labels") or {})
        labels.pop("pod-template-hash", None)
        template = {**template, "metadata": {**(template.get("metadata") or {}), "labels": labels}}
        await self.api.merge_patch(
            "deployments", n["app"], n["namespace"], {"spec": {"template": template}}
        )
        revision = await self._revision(n["namespace"], n["app"])
        image = template["spec"]["containers"][0]["image"]
        return Deployment(
            **base,
            image=image,
            revision=revision,
            previous_revision=plan.previous_revision,
            live_url=self._app_url(n["host"]),
            status="rolled_back",
            external_id=f"k8s:{self.cluster}:{n['namespace']}/deployments/{n['app']}",
        )
