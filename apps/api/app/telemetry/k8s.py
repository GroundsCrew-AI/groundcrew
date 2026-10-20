"""Kubernetes reader: a Deployment or StatefulSet, its revisions, pods, Warning events, pod
logs, and the ConfigMaps and Secrets its pods use (metadata only).

Read live (pods restart and events expire within the hour), through a `KubeReader`:

- the workload's conditions and replica counts (a Deployment's Progressing=False with
  ProgressDeadlineExceeded is a failed rollout; a StatefulSet whose currentRevision is not its
  updateRevision, with pods failing STUCK_AFTER after the new revision appeared, is stuck);
- its revisions: a Deployment's ReplicaSets (annotation deployment.kubernetes.io/revision), a
  StatefulSet's ControllerRevisions (`.revision`). Each one created in the window is a `deploy`
  change marker, ref "<namespace>/<name> revision <n>";
- its pods' container states: CrashLoopBackOff, image pull errors, OOMKilled or non-zero
  exits (last state), restarts, readiness;
- Warning events on the workload, its revisions, and its pods;
- the pods' recent log lines (timestamps from the kubelet), for the error-spike rule and the
  evidence;
- the ConfigMaps and Secrets its pod template uses (envFrom, env valueFrom, volumes), read as
  metadata only (`KubeReader.list_metadata`: no value ever reaches Groundcrew). One changed in
  the window (its last managedFields time) is a `config` (ConfigMap) or `secret_change`
  (Secret) change marker, ref "<namespace>/configmap/<name>@<resourceVersion>".

Shared by the watcher's rules (app/incidents/k8s_rules.py), the evidence collector, Medic's
verify, and the rollout actions (app/actions/k8s.py).
"""

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.agents.medic.events import ChangeMarker
from app.agents.medic.schemas import ChangeKind
from app.connectors.kubernetes import KubeReader
from app.discovery.types import ConnectorError
from app.telemetry.evidence import ReaderOutput, line, parse_time

SOURCE = "k8s"
REVISION = "deployment.kubernetes.io/revision"
# Pod-template labels Kubernetes adds per ReplicaSet; a rollback copies the template without it.
POD_TEMPLATE_HASH = "pod-template-hash"
# Container waiting reasons that mean the pod cannot run.
BROKEN_WAITING = frozenset(
    {
        "CrashLoopBackOff",
        "ImagePullBackOff",
        "ErrImagePull",
        "InvalidImageName",
        "CreateContainerConfigError",
        "CreateContainerError",
        "RunContainerError",
    }
)
MAX_LOG_PODS = 5
MAX_LOG_LINES_PER_POD = 1_000
# A StatefulSet rollout whose pods still fail this long after its new revision appeared is stuck
# (StatefulSets have no progress deadline).
STUCK_AFTER = timedelta(minutes=5)

DEPLOYMENT = "deployment"
STATEFULSET = "statefulset"
# Workload kind -> (the kind listed, its revisions' kind, the revisions' owner kind).
KINDS: dict[str, tuple[str, str, str]] = {
    DEPLOYMENT: ("deployments", "replicasets", "Deployment"),
    STATEFULSET: ("statefulsets", "controllerrevisions", "StatefulSet"),
}
# Graph resource type -> workload kind.
RESOURCE_KINDS: dict[str, str] = {"k8s:deployment": DEPLOYMENT, "k8s:statefulset": STATEFULSET}


def check_workload_kind(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"Not a Kubernetes workload kind: {kind}")
    return kind


@dataclass
class Workload:
    """One Deployment or StatefulSet as read now. `revisions` are its ReplicaSets or
    ControllerRevisions, oldest first. `configmaps` and `secrets` (metadata only) are filled by
    `fetch_config`; `config_notes` say what could not be read."""

    obj: dict[str, Any]
    revisions: list[dict[str, Any]] = field(default_factory=list)
    pods: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    kind: str = DEPLOYMENT
    configmaps: list[dict[str, Any]] = field(default_factory=list)
    secrets: list[dict[str, Any]] = field(default_factory=list)
    config_notes: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return str((self.obj.get("metadata") or {}).get("name") or self.kind)

    @property
    def namespace(self) -> str:
        return str((self.obj.get("metadata") or {}).get("namespace") or "default")

    @property
    def label(self) -> str:
        """ "Deployment" or "StatefulSet"."""
        return KINDS[self.kind][2]

    @property
    def template(self) -> dict[str, Any]:
        return dict((self.obj.get("spec") or {}).get("template") or {})


def revision(obj: dict[str, Any]) -> int | None:
    """A ReplicaSet's (or Deployment's) revision annotation, or a ControllerRevision's
    `.revision`."""
    value = ((obj.get("metadata") or {}).get("annotations") or {}).get(REVISION)
    if value is None:
        value = obj.get("revision")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def current_revision(workload: Workload) -> int | None:
    """The revision the workload's template is: a Deployment's annotation, a StatefulSet's
    updateRevision (the ControllerRevision it names)."""
    if workload.kind == DEPLOYMENT:
        return revision(workload.obj)
    update = (workload.obj.get("status") or {}).get("updateRevision")
    for rev in workload.revisions:
        if (rev.get("metadata") or {}).get("name") == update:
            return revision(rev)
    return max((r for r in map(revision, workload.revisions) if r is not None), default=None)


def revision_template(item: dict[str, Any]) -> dict[str, Any]:
    """The pod template a revision holds: a ReplicaSet's spec.template, a ControllerRevision's
    data.spec.template (without the "$patch": "replace" marker the StatefulSet controller
    stores in it)."""
    if "data" in item:
        template = copy.deepcopy(((item.get("data") or {}).get("spec") or {}).get("template") or {})
        template.pop("$patch", None)
        return dict(template)
    return dict((item.get("spec") or {}).get("template") or {})


def change_ref(namespace: str, name: str, rev: int | None) -> str:
    """The deploy change marker's ref, also an `ActionPlan.current` for a rollout undo."""
    return f"{namespace}/{name} revision {rev if rev is not None else '?'}"


def config_ref(namespace: str, kind: str, name: str, resource_version: Any) -> str:
    """A ConfigMap or Secret change marker's ref: "<ns>/configmap/<name>@<resourceVersion>"."""
    return f"{namespace}/{kind}/{name}@{resource_version}"


def images(template: dict[str, Any]) -> str:
    containers = ((template or {}).get("spec") or {}).get("containers") or []
    return ", ".join(str(c.get("image")) for c in containers) or "?"


def owned(
    revisions: list[dict[str, Any]], name: str, owner: str = "Deployment"
) -> list[dict[str, Any]]:
    """The workload's ReplicaSets (owner Deployment) or ControllerRevisions (owner
    StatefulSet), oldest revision first."""
    mine = [
        rs
        for rs in revisions
        if any(
            o.get("kind") == owner and o.get("name") == name
            for o in (rs.get("metadata") or {}).get("ownerReferences") or []
        )
    ]
    return sorted(mine, key=lambda rs: revision(rs) or 0)


def readiness(obj: dict[str, Any]) -> tuple[int, int]:
    """(ready, desired) replicas of a Deployment or StatefulSet."""
    spec, status = obj.get("spec") or {}, obj.get("status") or {}
    desired = spec.get("replicas")
    return int(status.get("readyReplicas") or 0), int(1 if desired is None else desired)


def rollout_failed(deployment: dict[str, Any]) -> dict[str, Any] | None:
    """The Progressing condition when the rollout passed its progress deadline."""
    for c in (deployment.get("status") or {}).get("conditions") or []:
        if (
            c.get("type") == "Progressing"
            and c.get("status") == "False"
            and c.get("reason") == "ProgressDeadlineExceeded"
        ):
            return dict(c)
    return None


def _update_started(workload: Workload) -> datetime | None:
    """When a StatefulSet's update revision appeared (its ControllerRevision's creation)."""
    update = (workload.obj.get("status") or {}).get("updateRevision")
    for rev in workload.revisions:
        meta = rev.get("metadata") or {}
        if meta.get("name") == update:
            return parse_time(meta.get("creationTimestamp"))
    return None


def statefulset_stuck(
    workload: Workload, failing: bool, now: datetime
) -> tuple[str, datetime] | None:
    """A StatefulSet rollout that is not complete (currentRevision != updateRevision) while its
    pods fail, STUCK_AFTER or more after the update revision appeared: (detail, when it began)."""
    if workload.kind != STATEFULSET or not failing:
        return None
    status = workload.obj.get("status") or {}
    current, update = status.get("currentRevision"), status.get("updateRevision")
    if not current or not update or current == update:
        return None
    started = _update_started(workload)
    if started is None or now - started < STUCK_AFTER:
        return None
    _, desired = readiness(workload.obj)
    updated = int(status.get("updatedReplicas") or 0)
    minutes = int((now - started).total_seconds() // 60)
    return (
        f"rollout stuck: {updated}/{desired} pods on revision {current_revision(workload)} "
        f"after {minutes} min, pods failing",
        started,
    )


def rollout_complete(workload: Workload) -> bool:
    """A StatefulSet: currentRevision == updateRevision and every replica ready. A Deployment:
    no failed rollout and every replica ready."""
    ready, desired = readiness(workload.obj)
    if workload.kind == STATEFULSET:
        status = workload.obj.get("status") or {}
        return status.get("currentRevision") == status.get("updateRevision") and ready >= desired
    return rollout_failed(workload.obj) is None and ready >= desired


def rolling_out(obj: dict[str, Any], kind: str = DEPLOYMENT) -> str | None:
    """Why the rollout is still going (verify waits), or None when it is complete."""
    meta, status = obj.get("metadata") or {}, obj.get("status") or {}
    if int(status.get("observedGeneration") or 0) < int(meta.get("generation") or 0):
        return "rollout not observed yet"
    ready, desired = readiness(obj)
    updated = int(status.get("updatedReplicas") or 0)
    if kind == STATEFULSET:
        if status.get("updateRevision") and status.get("currentRevision") != status.get(
            "updateRevision"
        ):
            return f"rollout in progress: {updated}/{desired} pods updated"
        if ready < desired:
            return f"rollout in progress: {ready}/{desired} pods ready"
        return None
    available = int(status.get("availableReplicas") or 0)
    total = int(status.get("replicas") or 0)
    if updated < desired:
        return f"rollout in progress: {updated}/{desired} pods updated"
    if total > updated:
        return f"rollout in progress: {total - updated} old pods terminating"
    if available < updated:
        return f"rollout in progress: {available}/{updated} pods available"
    return None


@dataclass
class PodProblem:
    pod: str
    container: str
    # CrashLoopBackOff, OOMKilled, ImagePullBackOff, "exit code 1", "not ready", ...
    reason: str
    at: datetime | None
    restarts: int = 0
    message: str = ""

    @property
    def crash(self) -> bool:
        return self.reason != "not ready"

    def text(self) -> str:
        out = f"pod {self.pod} container {self.container}: {self.reason}"
        if self.restarts:
            out += f", {self.restarts} restarts"
        if self.message:
            out += f" ({self.message[:200]})"
        return out


def _ready_since(pod: dict[str, Any]) -> tuple[bool, datetime | None]:
    for c in (pod.get("status") or {}).get("conditions") or []:
        if c.get("type") == "Ready":
            return c.get("status") == "True", parse_time(c.get("lastTransitionTime"))
    return False, None


def pod_problems(pods: list[dict[str, Any]], now: datetime, recent: timedelta) -> list[PodProblem]:
    """What is wrong with each pod now: a broken waiting state, a recent non-zero exit (its
    last state), or (running, older than a minute) failing its readiness probe."""
    out: list[PodProblem] = []
    for pod in pods:
        name = str((pod.get("metadata") or {}).get("name"))
        status = pod.get("status") or {}
        found = False
        for c in status.get("containerStatuses") or []:
            waiting = (c.get("state") or {}).get("waiting") or {}
            last = (c.get("lastState") or {}).get("terminated") or {}
            finished = parse_time(last.get("finishedAt"))
            restarts = int(c.get("restartCount") or 0)
            exited = last.get("exitCode") not in (None, 0)
            if waiting.get("reason") in BROKEN_WAITING:
                reason = str(waiting["reason"])
                if exited:
                    reason += f" (last exit {last.get('reason') or 'code'} {last.get('exitCode')})"
                at = finished or parse_time(status.get("startTime"))
                out.append(
                    PodProblem(
                        name,
                        str(c.get("name")),
                        reason,
                        at,
                        restarts,
                        str(waiting.get("message") or ""),
                    )
                )
                found = True
            elif exited and finished is not None and now - recent <= finished <= now:
                reason = (
                    "OOMKilled"
                    if last.get("reason") == "OOMKilled" or last.get("exitCode") == 137
                    else f"exited with code {last.get('exitCode')}"
                )
                out.append(
                    PodProblem(
                        name,
                        str(c.get("name")),
                        reason,
                        finished,
                        restarts,
                        str(last.get("message") or ""),
                    )
                )
                found = True
        if found or status.get("phase") != "Running":
            continue
        ready, since = _ready_since(pod)
        started = parse_time(status.get("startTime"))
        if not ready and started is not None and now - started > timedelta(minutes=1):
            out.append(PodProblem(name, "-", "not ready", since or started))
    return out


def parse_logs(text: str) -> list[tuple[datetime, str]]:
    """`kubectl logs --timestamps` lines: "<RFC 3339 time> <message>"."""
    out: list[tuple[datetime, str]] = []
    for raw in text.splitlines():
        stamp, _, message = raw.partition(" ")
        at = parse_time(stamp)
        if at is not None:
            out.append((at, message))
    return out


def _selector(obj: dict[str, Any]) -> str:
    labels = ((obj.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


async def fetch_workload(
    reader: KubeReader, namespace: str, name: str, kind: str = DEPLOYMENT
) -> Workload | None:
    """The Deployment (or StatefulSet), its ReplicaSets (or ControllerRevisions) and pods, and
    the namespace's Warning events about them."""
    listed, revisions_kind, owner = KINDS[check_workload_kind(kind)]
    found = await reader.list(listed, namespace, field_selector=f"metadata.name={name}")
    if not found:
        return None
    obj = found[0]
    selector = _selector(obj)
    pods = await reader.list("pods", namespace, label_selector=selector) if selector else []
    revisions = owned(
        await reader.list(revisions_kind, namespace, label_selector=selector or None), name, owner
    )
    events = await reader.list("events", namespace, field_selector="type=Warning")
    mine = {name, *(str((p.get("metadata") or {}).get("name")) for p in pods)}
    mine |= {str((rs.get("metadata") or {}).get("name")) for rs in revisions}
    return Workload(
        obj=obj,
        revisions=revisions,
        pods=pods,
        events=[e for e in events if str((e.get("involvedObject") or {}).get("name")) in mine],
        kind=kind,
    )


def config_refs(template: dict[str, Any]) -> tuple[set[str], set[str]]:
    """The ConfigMaps and Secrets a pod template uses, by name: containers' and init
    containers' envFrom and env valueFrom, and volumes (projected ones included)."""
    spec = (template or {}).get("spec") or {}
    configmaps: set[str] = set()
    secrets: set[str] = set()
    for c in [*(spec.get("containers") or []), *(spec.get("initContainers") or [])]:
        for source in c.get("envFrom") or []:
            if (source.get("configMapRef") or {}).get("name"):
                configmaps.add(str(source["configMapRef"]["name"]))
            if (source.get("secretRef") or {}).get("name"):
                secrets.add(str(source["secretRef"]["name"]))
        for env in c.get("env") or []:
            value_from = env.get("valueFrom") or {}
            if (value_from.get("configMapKeyRef") or {}).get("name"):
                configmaps.add(str(value_from["configMapKeyRef"]["name"]))
            if (value_from.get("secretKeyRef") or {}).get("name"):
                secrets.add(str(value_from["secretKeyRef"]["name"]))
    for volume in spec.get("volumes") or []:
        if (volume.get("configMap") or {}).get("name"):
            configmaps.add(str(volume["configMap"]["name"]))
        if (volume.get("secret") or {}).get("secretName"):
            secrets.add(str(volume["secret"]["secretName"]))
        for source in (volume.get("projected") or {}).get("sources") or []:
            if (source.get("configMap") or {}).get("name"):
                configmaps.add(str(source["configMap"]["name"]))
            if (source.get("secret") or {}).get("name"):
                secrets.add(str(source["secret"]["name"]))
    return configmaps, secrets


def changed_at(item: dict[str, Any]) -> datetime | None:
    """When a ConfigMap or Secret last changed: its newest managedFields time, else its
    creation time."""
    meta = item.get("metadata") or {}
    times = [parse_time(f.get("time")) for f in meta.get("managedFields") or []]
    known = [t for t in times if t is not None]
    return max(known) if known else parse_time(meta.get("creationTimestamp"))


# The note when a workload uses ConfigMaps but the connection has not opted in.
CONFIGMAPS_OFF_NOTE = (
    'ConfigMap changes not visible (opt-in): turn on "Detect ConfigMap changes" on the '
    "Kubernetes connection, with the optional ConfigMap rule in infra/k8s-readonly-rbac.yaml"
)


async def fetch_config(
    reader: KubeReader, workload: Workload, *, configmaps_enabled: bool = False
) -> Workload:
    """Fills the workload's `configmaps` and `secrets`: the metadata of the ones its pod template
    uses (`list_metadata`: names, resourceVersion, managedFields times; never data). ConfigMaps
    are read only when the connection opted in (`configmap_changes`); otherwise a note says
    they are not visible. When the kubeconfig may not list them (the optional rules in
    infra/k8s-readonly-rbac.yaml), they are skipped with a note."""
    configmaps, secrets = config_refs(workload.template)
    for kind, label, names, into in (
        ("configmaps", "ConfigMap", configmaps, workload.configmaps),
        ("secrets", "Secret", secrets, workload.secrets),
    ):
        if not names:
            continue
        if kind == "configmaps" and not configmaps_enabled:
            workload.config_notes.append(CONFIGMAPS_OFF_NOTE)
            continue
        try:
            items = await reader.list_metadata(kind, workload.namespace)
        except ConnectorError as exc:
            workload.config_notes.append(f"{label} changes not checked: {exc}")
            continue
        into.extend(i for i in items if str((i.get("metadata") or {}).get("name")) in names)
    return workload


async def fetch_logs(
    reader: KubeReader, workload: Workload, since_seconds: int
) -> list[tuple[datetime, str]]:
    """The newest pods' log lines (MAX_LOG_PODS pods, each capped), oldest first. A pod whose
    logs cannot be read is skipped."""
    pods = sorted(
        workload.pods,
        key=lambda p: str((p.get("metadata") or {}).get("creationTimestamp") or ""),
        reverse=True,
    )[:MAX_LOG_PODS]
    out: list[tuple[datetime, str]] = []
    for pod in pods:
        try:
            text = await reader.pod_logs(
                workload.namespace,
                str((pod.get("metadata") or {}).get("name")),
                since_seconds=since_seconds,
                tail_lines=MAX_LOG_LINES_PER_POD,
            )
        except Exception:  # noqa: S112 (a pod gone or not started: the others still count)
            continue
        out.extend(parse_logs(text))
    return sorted(out, key=lambda e: e[0])


def _in(at: datetime | None, since: datetime, until: datetime) -> bool:
    return at is not None and since <= at <= until


def _config_changes(
    workload: Workload, since: datetime, until: datetime, out: ReaderOutput
) -> None:
    name, ns = workload.name, workload.namespace
    sources: list[tuple[str, str, ChangeKind, list[dict[str, Any]]]] = [
        ("configmap", "ConfigMap", "config", workload.configmaps),
        ("secret", "Secret", "secret_change", workload.secrets),
    ]
    for kind, label, marker_kind, items in sources:
        for item in items:
            meta = item.get("metadata") or {}
            at = changed_at(item)
            if not _in(at, since, until) or at is None:
                continue
            obj, version = str(meta.get("name")), meta.get("resourceVersion") or "?"
            out.lines.append(
                line(
                    at,
                    SOURCE,
                    name,
                    f"{label} {obj} (used by {name}) changed, resourceVersion {version}",
                )
            )
            out.changes.append(
                ChangeMarker(
                    kind=marker_kind,
                    ref=config_ref(ns, kind, obj, version),
                    at=at,
                    label=f"{label} {ns}/{obj} changed",
                )
            )
    for note in workload.config_notes:
        out.lines.append(line(until, SOURCE, name, note))


def describe(
    workload: Workload,
    logs: list[tuple[datetime, str]],
    since: datetime,
    until: datetime,
) -> ReaderOutput:
    """Evidence lines and change markers (deploy, config, secret_change) for one workload."""
    name, ns = workload.name, workload.namespace
    out = ReaderOutput()
    for rs in workload.revisions:
        created = parse_time((rs.get("metadata") or {}).get("creationTimestamp"))
        rev = revision(rs)
        template = revision_template(rs)
        if _in(created, since, until) and created is not None:
            out.lines.append(
                line(created, SOURCE, name, f"revision {rev} rolled out: image {images(template)}")
            )
            out.changes.append(
                ChangeMarker(
                    kind="deploy",
                    ref=change_ref(ns, name, rev),
                    at=created,
                    label=f"{name} revision {rev} ({images(template)})",
                )
            )
    _config_changes(workload, since, until, out)
    failed = rollout_failed(workload.obj)
    if failed is not None:
        at = parse_time(failed.get("lastUpdateTime")) or parse_time(
            failed.get("lastTransitionTime")
        )
        out.lines.append(
            line(
                max(min(at or until, until), since),
                SOURCE,
                name,
                f"rollout failed: {failed.get('message') or 'progress deadline exceeded'}",
            )
        )
    problems = pod_problems(workload.pods, until, until - since)
    stuck = statefulset_stuck(workload, bool(problems), until)
    if stuck is not None:
        out.lines.append(line(max(min(stuck[1], until), since), SOURCE, name, stuck[0]))
    for e in workload.events:
        at = parse_time(e.get("lastTimestamp")) or parse_time(e.get("eventTime"))
        if not _in(at, since, until) or at is None:
            continue
        obj = e.get("involvedObject") or {}
        count = e.get("count") or 1
        out.lines.append(
            line(
                at,
                SOURCE,
                name,
                f"event {e.get('reason')} on {obj.get('kind')} {obj.get('name')}"
                + (f" (x{count})" if count > 1 else "")
                + f": {str(e.get('message') or '')[:300]}",
            )
        )
    for problem in problems:
        out.lines.append(
            line(max(min(problem.at or until, until), since), SOURCE, name, problem.text())
        )
    for at, message in logs:
        if _in(at, since, until):
            out.lines.append(line(at, f"{SOURCE}_logs", name, message))
    ready, desired = readiness(workload.obj)
    out.lines.append(
        line(
            until,
            SOURCE,
            name,
            f"now: revision {current_revision(workload)}, {ready}/{desired} pods ready, image "
            f"{images(workload.template)}",
        )
    )
    return out


async def read_workload(
    reader: KubeReader,
    namespace: str,
    name: str,
    since: datetime,
    until: datetime,
    kind: str = DEPLOYMENT,
    *,
    configmaps_enabled: bool = False,
) -> ReaderOutput:
    workload = await fetch_workload(reader, namespace, name, kind)
    if workload is None:
        return ReaderOutput(lines=[line(until, SOURCE, name, f"{kind} not found")])
    await fetch_config(reader, workload, configmaps_enabled=configmaps_enabled)
    seconds = max(60, int((until - since).total_seconds()) + 60)
    logs = await fetch_logs(reader, workload, seconds)
    return describe(workload, logs, since, until)
