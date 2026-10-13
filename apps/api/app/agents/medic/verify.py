"""Recovery checks after a fix: the same signals that opened the incident, read again.

For each affected (or acted-on) ECS service: `rollout` passes when every desired task runs and
the primary deployment has not failed; `health` / `errors` fail on tasks stopped since the fix
for failing health checks or with a non-zero exit. A log error spike seen at detection is read
again (`errors`); an RDS spike or slow queries are read again (`metric`). Medic resolves only
after two healthy rounds in a row (MedicRunner), so one lucky read does not close an incident.

A round is *waiting*, not failing, while a service's rollout is IN_PROGRESS (more than one
deployment) or, after a fix PR merged, no deployment has started since the merge: the checks
then describe the old tasks, so the UI shows "waiting for rollout" until the deadline."""

import time
from datetime import datetime
from typing import Any

from app.connectors.aws import AwsReader, error_text
from app.db.models import Resource
from app.incidents import rules
from app.pipeline.events import VerifyCheck, VerifyResult


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
    after = [t for t in tasks if (rules.to_time(t.get("stoppedAt")) or since) > since]
    health = [t for t in after if rules.HEALTH.search(str(t.get("stoppedReason", "")))]
    # A running task can fail every health check without being stopped yet.
    health_events = [
        e
        for e in service.get("events", [])
        if (rules.to_time(e.get("createdAt")) or since) > since
        and rules.HEALTH.search(str(e.get("message", "")))
        and "fail" in str(e.get("message", "")).lower()
    ]
    crashed = [
        t
        for t in after
        if t not in health
        and any(c.get("exitCode") not in (None, 0) for c in t.get("containers", []))
    ]
    out.append(
        VerifyCheck(
            name="health",
            ok=not health and not health_events,
            detail=(
                f"{svc.name}: {len(health)} tasks failed health checks since the fix, "
                f"{len(health_events)} health-check failures reported by ECS"
            ),
        )
    )
    out.append(
        VerifyCheck(
            name="errors",
            ok=not crashed,
            detail=f"{svc.name}: {len(crashed)} tasks exited non-zero since the fix",
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
) -> VerifyResult:
    """Blocking (boto3). `readers` is keyed by connection id (str). `deploy_after`: a fix PR
    merged then; `waiting`: reasons known before the round (a fix PR not merged yet)."""
    started = time.monotonic()
    checks: list[VerifyCheck] = []
    waits = list(waiting or [])
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
            spike = rules.rds_check(reader, db.region, db.id, db.name, groups, now)
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
