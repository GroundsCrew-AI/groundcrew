"""Kubernetes reader: a Deployment, its revisions, pods, Warning events, and pod logs.

Read live (pods restart and events expire within the hour), through a `KubeReader`:

- the Deployment's conditions and replica counts (Progressing=False with
  ProgressDeadlineExceeded is a failed rollout);
- its ReplicaSets, one per revision (annotation deployment.kubernetes.io/revision): each one
  created in the window is a `deploy` change marker, ref "<namespace>/<name> revision <n>";
- its pods' container states: CrashLoopBackOff, image pull errors, OOMKilled or non-zero
  exits (last state), restarts, readiness;
- Warning events on the Deployment, its ReplicaSets, and its pods;
- the pods' recent log lines (timestamps from the kubelet), for the error-spike rule and the
  evidence.

Shared by the watcher's rules (app/incidents/k8s_rules.py), the evidence collector, Medic's
verify, and the rollout actions (app/actions/k8s.py).
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.agents.medic.events import ChangeMarker
from app.connectors.kubernetes import KubeReader
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


@dataclass
class Workload:
    """One Deployment as read now."""

    deployment: dict[str, Any]
    replicasets: list[dict[str, Any]] = field(default_factory=list)
    pods: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return str((self.deployment.get("metadata") or {}).get("name") or "deployment")

    @property
    def namespace(self) -> str:
        return str((self.deployment.get("metadata") or {}).get("namespace") or "default")


def revision(obj: dict[str, Any]) -> int | None:
    value = ((obj.get("metadata") or {}).get("annotations") or {}).get(REVISION)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def change_ref(namespace: str, name: str, rev: int | None) -> str:
    """The deploy change marker's ref, also an `ActionPlan.current` for a rollout undo."""
    return f"{namespace}/{name} revision {rev if rev is not None else '?'}"


def images(template: dict[str, Any]) -> str:
    containers = ((template or {}).get("spec") or {}).get("containers") or []
    return ", ".join(str(c.get("image")) for c in containers) or "?"


def owned(replicasets: list[dict[str, Any]], deployment: str) -> list[dict[str, Any]]:
    """The Deployment's ReplicaSets, oldest revision first."""
    mine = [
        rs
        for rs in replicasets
        if any(
            o.get("kind") == "Deployment" and o.get("name") == deployment
            for o in (rs.get("metadata") or {}).get("ownerReferences") or []
        )
    ]
    return sorted(mine, key=lambda rs: revision(rs) or 0)


def readiness(deployment: dict[str, Any]) -> tuple[int, int]:
    """(ready, desired) replicas."""
    spec, status = deployment.get("spec") or {}, deployment.get("status") or {}
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


def rolling_out(deployment: dict[str, Any]) -> str | None:
    """Why the rollout is still going (verify waits), or None when it is complete."""
    meta, status = deployment.get("metadata") or {}, deployment.get("status") or {}
    if int(status.get("observedGeneration") or 0) < int(meta.get("generation") or 0):
        return "rollout not observed yet"
    _, desired = readiness(deployment)
    updated = int(status.get("updatedReplicas") or 0)
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


def _selector(deployment: dict[str, Any]) -> str:
    labels = ((deployment.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


async def fetch_workload(reader: KubeReader, namespace: str, name: str) -> Workload | None:
    """The Deployment, its ReplicaSets and pods, and the namespace's Warning events about them."""
    found = await reader.list("deployments", namespace, field_selector=f"metadata.name={name}")
    if not found:
        return None
    deployment = found[0]
    selector = _selector(deployment)
    pods = await reader.list("pods", namespace, label_selector=selector) if selector else []
    replicasets = await reader.list("replicasets", namespace, label_selector=selector or None)
    events = await reader.list("events", namespace, field_selector="type=Warning")
    mine = {name, *(str((p.get("metadata") or {}).get("name")) for p in pods)}
    mine |= {str((rs.get("metadata") or {}).get("name")) for rs in owned(replicasets, name)}
    return Workload(
        deployment=deployment,
        replicasets=owned(replicasets, name),
        pods=pods,
        events=[e for e in events if str((e.get("involvedObject") or {}).get("name")) in mine],
    )


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


def describe(
    workload: Workload,
    logs: list[tuple[datetime, str]],
    since: datetime,
    until: datetime,
) -> ReaderOutput:
    """Evidence lines and deploy change markers for one Deployment."""
    name, ns = workload.name, workload.namespace
    out = ReaderOutput()
    for rs in workload.replicasets:
        created = parse_time((rs.get("metadata") or {}).get("creationTimestamp"))
        rev = revision(rs)
        template = (rs.get("spec") or {}).get("template") or {}
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
    failed = rollout_failed(workload.deployment)
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
    for problem in pod_problems(workload.pods, until, until - since):
        out.lines.append(
            line(max(min(problem.at or until, until), since), SOURCE, name, problem.text())
        )
    for at, message in logs:
        if _in(at, since, until):
            out.lines.append(line(at, f"{SOURCE}_logs", name, message))
    ready, desired = readiness(workload.deployment)
    current = revision(workload.deployment)
    out.lines.append(
        line(
            until,
            SOURCE,
            name,
            f"now: revision {current}, {ready}/{desired} pods ready, image "
            f"{images((workload.deployment.get('spec') or {}).get('template') or {})}",
        )
    )
    return out


async def read_workload(
    reader: KubeReader, namespace: str, name: str, since: datetime, until: datetime
) -> ReaderOutput:
    workload = await fetch_workload(reader, namespace, name)
    if workload is None:
        return ReaderOutput(lines=[line(until, SOURCE, name, "deployment not found")])
    seconds = max(60, int((until - since).total_seconds()) + 60)
    logs = await fetch_logs(reader, workload, seconds)
    return describe(workload, logs, since, until)
