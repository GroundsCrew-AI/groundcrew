"""Recovery checks after a fix: the same signals that opened the incident, read again.

For each affected (or acted-on) ECS service: `rollout` passes when every desired task runs and
the primary deployment has not failed; `health` / `errors` fail on tasks *started* since the fix
that stopped for failing health checks or with a non-zero exit, and on running tasks that are
UNHEALTHY now. Each task is judged by its own record (service_tasks): ECS service events only
point at the tasks to read, because they outlive the tasks they describe. A log error spike
seen at detection is read again (`errors`); an RDS spike or slow queries are read again
(`metric`). Medic resolves only after two healthy rounds in a row (MedicRunner), so one lucky
read does not close an incident.

A round is *waiting*, not failing, while a service's rollout is IN_PROGRESS (more than one
deployment) or, after a fix PR merged, no deployment has started since the merge: the checks
then describe the old tasks, so the UI shows "waiting for rollout" until the deadline.

Kubernetes Deployments and StatefulSets (`k8s_checks`): `rollout` passes when the rollout is
complete and every desired pod is ready (a StatefulSet: currentRevision == updateRevision and
readyReplicas == replicas), waiting while pods are still being replaced, or after a fix PR merged
until a revision created since the merge exists; `health` fails on any pod that crashes, cannot
start, or fails its readiness probe now (pods being deleted are left out); `errors` reads the
pods' logs again when a log spike opened the incident.

Services a Groundcrew agent watches (`agent_checks`): `health` passes when the agent's report
after the fix shows the service running with no failed health check in a row; until the agent
reports again after the fix, the round is waiting."""

import re
import time
from datetime import datetime
from typing import Any

from app.connectors.aws import AwsReader, error_text
from app.connectors.kubernetes import KubeReader
from app.db.models import Resource
from app.incidents import rules
from app.pipeline.events import VerifyCheck, VerifyResult
from app.telemetry import k8s
from app.telemetry.evidence import parse_time


def rollout_wait(
    name: str, service: dict[str, Any], deploy_after: datetime | None = None
) -> str | None:
    """Why the service's checks cannot judge the fix yet, or None."""
    deployments = service.get("deployments", [])
    primary: dict[str, Any] = next((d for d in deployments if d.get("status") == "PRIMARY"), {})
    state = primary.get("rolloutState")
    if state == "IN_PROGRESS" or len(deployments) > 1:
        return f"{name} rollout {state or 'IN_PROGRESS'}"
    started = rules.to_time(primary.get("createdAt"))
    if deploy_after is not None and (started is None or started < deploy_after):
        return f"{name}: no deploy since the fix PR merged"
    return None


_TASK_ID = re.compile(r"\(task ([0-9a-f]{12,})\)")
_CHUNK = 100


def _task_id(task: dict[str, Any]) -> str:
    return str(task.get("taskArn", "")).rsplit("/", 1)[-1]


def service_tasks(
    reader: AwsReader,
    region: str | None,
    cluster: str,
    service: dict[str, Any],
    stopped: list[dict[str, Any]],
    since: datetime,
) -> list[dict[str, Any]]:
    """Blocking (boto3). Every task verify must judge, each with its own record: the stopped
    ones rules.fetch_ecs listed (one capped page, in no set order), the service's running tasks,
    and every task a health-check failure event since the fix names that is in neither (ECS
    keeps a stopped task describable for an hour). Service events are only pointers to tasks."""
    known = {_task_id(t): t for t in stopped}
    running = reader.paginate(
        "ecs",
        region,
        "ListTasks",
        "taskArns",
        cluster=cluster,
        serviceName=str(service.get("serviceName") or ""),
        desiredStatus="RUNNING",
    )
    wanted = [str(a).rsplit("/", 1)[-1] for a in running]
    for event in service.get("events", []):
        at = rules.to_time(event.get("createdAt"))
        message = str(event.get("message", ""))
        found = _TASK_ID.search(message)
        if found and at is not None and at > since and rules.HEALTH.search(message):
            wanted.append(found.group(1))
    missing = [i for i in dict.fromkeys(wanted) if i not in known]
    for i in range(0, len(missing), _CHUNK):
        described = reader.call(
            "ecs", region, "DescribeTasks", cluster=cluster, tasks=missing[i : i + _CHUNK]
        ).get("tasks", [])
        for task in described:
            known[_task_id(task)] = task
    return list(known.values())


def _started(task: dict[str, Any]) -> datetime | None:
    return rules.to_time(task.get("startedAt")) or rules.to_time(task.get("createdAt"))


def started_since(task: dict[str, Any], since: datetime) -> bool:
    """A task of the fix: started after it. A task the fix replaced (running the broken config
    when the fix landed) may fail, exit, or be reported unhealthy afterwards; that is not the
    fix failing."""
    started = _started(task)
    return started is not None and started > since


def health_failures(
    tasks: list[dict[str, Any]], since: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Tasks started since the fix that stopped for failing health checks, and tasks running now
    (meant to keep running) whose health is UNHEALTHY."""
    stopped = [
        t
        for t in tasks
        if started_since(t, since)
        and t.get("lastStatus") == "STOPPED"
        and rules.HEALTH.search(str(t.get("stoppedReason", "")))
    ]
    unhealthy = [
        t
        for t in tasks
        if t.get("lastStatus") == "RUNNING"
        and t.get("desiredStatus", "RUNNING") == "RUNNING"
        and t.get("healthStatus") == "UNHEALTHY"
    ]
    return stopped, unhealthy


def _service_checks(
    reader: AwsReader,
    svc: Resource,
    since: datetime,
    now: datetime,
    log_groups: list[str],
    check_logs: bool,
    waits: list[str],
    deploy_after: datetime | None = None,
) -> list[VerifyCheck]:
    cluster = str(svc.config.get("cluster_arn") or "")
    service, tasks = rules.fetch_ecs(reader, svc.region, cluster, svc.name)
    if service is None:
        return [VerifyCheck(name="rollout", ok=False, detail=f"{svc.name}: service not found")]
    wait = rollout_wait(svc.name, service, deploy_after)
    if wait is not None:
        waits.append(wait)
    running, desired = service.get("runningCount", 0), service.get("desiredCount", 0)
    primary: dict[str, Any] = next(
        (d for d in service.get("deployments", []) if d.get("status") == "PRIMARY"), {}
    )
    failed = primary.get("rolloutState") == "FAILED"
    ok = (
        desired > 0
        and running >= desired
        and not failed
        and len(service.get("deployments", [])) == 1
    )
    rollout = primary.get("rolloutState") or "-"
    detail = f"{svc.name}: {running}/{desired} tasks running, rollout {rollout}"
    out = [VerifyCheck(name="rollout", ok=ok, detail=detail)]
    tasks = service_tasks(reader, svc.region, cluster, service, tasks, since)
    health, unhealthy = health_failures(tasks, since)
    # Only tasks of the fix: one the restart replaced exits non-zero on its way out.
    crashed = [
        t
        for t in tasks
        if started_since(t, since)
        and t.get("lastStatus") == "STOPPED"
        and t not in health
        and any(c.get("exitCode") not in (None, 0) for c in t.get("containers", []))
    ]
    out.append(
        VerifyCheck(
            name="health",
            ok=not health and not unhealthy,
            detail=(
                f"{svc.name}: {len(health)} tasks started since the fix failed health checks, "
                f"{len(unhealthy)} running tasks unhealthy"
            ),
        )
    )
    out.append(
        VerifyCheck(
            name="errors",
            ok=not crashed,
            detail=f"{svc.name}: {len(crashed)} tasks started since the fix exited non-zero",
        )
    )
    if check_logs:
        for group in log_groups:
            events = rules.fetch_logs(reader, svc.region, group, now)
            spike = rules.log_signal(svc.id, svc.name, events, now)
            out.append(
                VerifyCheck(
                    name="errors",
                    ok=spike is None,
                    detail=f"{group}: " + (spike.detail if spike else "error rate back to normal"),
                )
            )
    return out


def run_checks(
    readers: dict[str, AwsReader],
    services: list[Resource],
    databases: list[Resource],
    log_groups: dict[str, list[str]],
    *,
    since: datetime,
    now: datetime,
    check_logs: bool,
    deploy_after: datetime | None = None,
    waiting: list[str] | None = None,
    functions: list[Resource] | None = None,
    instances: list[Resource] | None = None,
    balancers: list[Resource] | None = None,
) -> VerifyResult:
    """Blocking (boto3). `readers` is keyed by connection id (str). `deploy_after`: a fix PR
    merged then; `waiting`: reasons known before the round (a fix PR not merged yet).
    `functions` (Lambda): errors and throttles back to normal; `instances` (EC2): running and
    both status checks passing."""
    started = time.monotonic()
    checks: list[VerifyCheck] = []
    waits = list(waiting or [])
    for lb in balancers or []:
        reader = readers.get(str(lb.connection_id))
        if reader is None:
            continue
        try:
            found = rules.alb_check(reader, lb.region, lb.id, lb.name, lb.config, now)
        except Exception as exc:  # one unreadable balancer must not stop the round
            detail = f"{lb.name}: could not read metrics ({error_text(exc)[:120]})"
            checks.append(VerifyCheck(name="errors", ok=False, detail=detail))
            continue
        checks.append(
            VerifyCheck(
                name="errors",
                ok=not found,
                detail=f"{lb.name}: "
                + ("; ".join(s.detail for s in found) or "5xx, targets, and latency normal"),
            )
        )
    for fn in functions or []:
        reader = readers.get(str(fn.connection_id))
        if reader is None:
            continue
        try:
            found = rules.lambda_signals(
                fn.id, fn.name, rules.fetch_lambda(reader, fn.region, fn.name, now), now
            )
        except Exception as exc:  # one unreadable function must not stop the round
            detail = f"{fn.name}: could not read metrics ({error_text(exc)[:120]})"
            checks.append(VerifyCheck(name="errors", ok=False, detail=detail))
            continue
        checks.append(
            VerifyCheck(
                name="errors",
                ok=not found,
                detail=f"{fn.name}: "
                + ("; ".join(s.detail for s in found) or "errors and throttles back to normal"),
            )
        )
    for inst in instances or []:
        reader = readers.get(str(inst.connection_id))
        if reader is None:
            continue
        try:
            status = rules.fetch_ec2_status(
                reader, inst.region, rules.instance_id(inst.external_id)
            )
        except Exception as exc:
            detail = f"{inst.name}: could not read status ({error_text(exc)[:120]})"
            checks.append(VerifyCheck(name="health", ok=False, detail=detail))
            continue
        state = ((status or {}).get("InstanceState") or {}).get("Name")
        impaired = rules.ec2_signal(inst.id, inst.name, status, now)
        initializing = any(
            ((status or {}).get(k) or {}).get("Status") == "initializing"
            for k in ("SystemStatus", "InstanceStatus")
        )
        if initializing:
            waits.append(f"{inst.name} status checks initializing after the reboot")
        checks.append(
            VerifyCheck(
                name="health",
                ok=state == "running" and impaired is None and not initializing,
                detail=f"{inst.name}: "
                + (impaired.detail if impaired else f"{state}, status checks passing"),
            )
        )
    for svc in services:
        reader = readers.get(str(svc.connection_id))
        if reader is None:
            checks.append(VerifyCheck(name="other", ok=False, detail=f"{svc.name}: no AWS access"))
            continue
        checks.extend(
            _service_checks(
                reader,
                svc,
                since,
                now,
                log_groups.get(str(svc.id), []),
                check_logs,
                waits,
                deploy_after,
            )
        )
    for db in databases:
        reader = readers.get(str(db.connection_id))
        if reader is None:
            continue
        groups = sorted({g for gs in log_groups.values() for g in gs})
        try:
            spike = rules.rds_check(
                reader,
                db.region,
                db.id,
                db.name,
                groups,
                now,
                "DBClusterIdentifier" if db.type == "rds:cluster" else "DBInstanceIdentifier",
            )
        except Exception as exc:  # one unreadable database must not stop the round
            detail = f"{db.name}: could not read metrics ({error_text(exc)[:120]})"
            checks.append(VerifyCheck(name="metric", ok=False, detail=detail))
            continue
        checks.append(
            VerifyCheck(
                name="metric",
                ok=spike is None,
                detail=f"{db.name}: " + (spike.detail if spike else "CPU and query latency normal"),
            )
        )
    return VerifyResult(
        ok=bool(checks) and all(c.ok for c in checks) and not waits,
        checks=checks,
        duration_ms=int((time.monotonic() - started) * 1000),
        waiting="; ".join(waits) or None,
    )


async def k8s_checks(
    reader: KubeReader,
    deployment: Resource,
    *,
    since: datetime,
    now: datetime,
    check_logs: bool,
    waits: list[str],
    deploy_after: datetime | None = None,
) -> list[VerifyCheck]:
    """One Deployment's or StatefulSet's recovery checks; appends to `waits` while its rollout
    is going."""
    name = deployment.name
    namespace = str(deployment.config.get("namespace") or deployment.region or "default")
    kind = k8s.RESOURCE_KINDS.get(deployment.type, k8s.DEPLOYMENT)
    workload = await k8s.fetch_workload(reader, namespace, name, kind)
    if workload is None:
        return [VerifyCheck(name="rollout", ok=False, detail=f"{name}: {kind} not found")]
    going = k8s.rolling_out(workload.obj, kind)
    if going:
        waits.append(f"{name} {going}")
    if deploy_after is not None:
        newest = max(
            (
                parse_time((rs.get("metadata") or {}).get("creationTimestamp"))
                for rs in workload.revisions
            ),
            default=None,
            key=lambda at: at.timestamp() if at else 0.0,
        )
        if newest is None or newest < deploy_after:
            waits.append(f"{name}: no new revision since the fix PR merged")
    ready, desired = k8s.readiness(workload.obj)
    failed = k8s.rollout_failed(workload.obj)
    rollout_ok = k8s.rollout_complete(workload)
    detail = f"{name}: {ready}/{desired} pods ready, revision {k8s.current_revision(workload)}"
    if kind == k8s.STATEFULSET and not rollout_ok and ready >= desired:
        detail += "; rollout not complete (currentRevision is not updateRevision)"
    if failed is not None:
        detail += f"; rollout failed: {failed.get('message') or 'progress deadline exceeded'}"
    out = [VerifyCheck(name="rollout", ok=rollout_ok, detail=detail)]
    live = [p for p in workload.pods if not (p.get("metadata") or {}).get("deletionTimestamp")]
    problems = k8s.pod_problems(live, now, now - since)
    out.append(
        VerifyCheck(
            name="health",
            ok=not problems,
            detail=f"{name}: "
            + (
                "; ".join(p.text() for p in problems[:3])
                if problems
                else "every pod running and ready"
            ),
        )
    )
    if check_logs:
        window = rules.RECENT + rules.BASELINE
        logs = await k8s.fetch_logs(reader, workload, int(window.total_seconds()))
        spike = rules.log_signal(deployment.id, name, logs, now)
        out.append(
            VerifyCheck(
                name="errors",
                ok=spike is None,
                detail=f"{name} logs: " + (spike.detail if spike else "error rate back to normal"),
            )
        )
    return out


def agent_checks(
    service: Resource,
    states: dict[str, Any],
    states_at: datetime | None,
    *,
    since: datetime,
    waits: list[str],
) -> list[VerifyCheck]:
    """The service's state as its agent last reported it (`agents.states`)."""
    from app.remote_agents.signals import RUNNING, ServiceState, describe

    raw = states.get(service.name)
    if states_at is None or states_at <= since or raw is None:
        waits.append(f"the agent's next report on {service.name}")
        return []
    s = ServiceState.model_validate(raw)
    running = s.found and s.state in RUNNING
    healthy = running and s.health_failures == 0
    detail = describe(s)
    if running and s.health_failures:
        detail += f", {s.health_failures} health checks failed in a row ({s.health_detail})"
    elif healthy:
        detail += ", health checks passing" if raw.get("health_failures") is not None else ""
    return [VerifyCheck(name="health", ok=healthy, detail=detail)]
