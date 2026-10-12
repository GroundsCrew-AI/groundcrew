"""Recovery checks after a fix: the same signals that opened the incident, read again.

For each affected (or acted-on) ECS service: `rollout` passes when every desired task runs and
the primary deployment has not failed; `health` / `errors` fail on tasks stopped since the fix
for failing health checks or with a non-zero exit. A log error spike seen at detection is read
again (`errors`); an RDS spike is read again (`metric`). Medic resolves only after two healthy
rounds in a row (MedicRunner), so one lucky read does not close an incident.
"""

import time
from datetime import datetime
from typing import Any

from app.connectors.aws import AwsReader
from app.db.models import Resource
from app.incidents import rules
from app.pipeline.events import VerifyCheck, VerifyResult


def _service_checks(
    reader: AwsReader,
    svc: Resource,
    since: datetime,
    now: datetime,
    log_groups: list[str],
    check_logs: bool,
) -> list[VerifyCheck]:
    cluster = str(svc.config.get("cluster_arn") or "")
    service, tasks = rules.fetch_ecs(reader, svc.region, cluster, svc.name)
    if service is None:
        return [VerifyCheck(name="rollout", ok=False, detail=f"{svc.name}: service not found")]
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
    crashed = [
        t
        for t in after
        if t not in health
        and any(c.get("exitCode") not in (None, 0) for c in t.get("containers", []))
    ]
    out.append(
        VerifyCheck(
            name="health",
            ok=not health,
            detail=f"{svc.name}: {len(health)} tasks failed health checks since the fix",
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
) -> VerifyResult:
    """Blocking (boto3). `readers` is keyed by connection id (str)."""
    started = time.monotonic()
    checks: list[VerifyCheck] = []
    for svc in services:
        reader = readers.get(str(svc.connection_id))
        if reader is None:
            checks.append(VerifyCheck(name="other", ok=False, detail=f"{svc.name}: no AWS access"))
            continue
        checks.extend(
            _service_checks(reader, svc, since, now, log_groups.get(str(svc.id), []), check_logs)
        )
    for db in databases:
        reader = readers.get(str(db.connection_id))
        if reader is None:
            continue
        cpu, latency = rules.fetch_rds(reader, db.region, db.name, now)
        spike = rules.rds_signal(db.id, db.name, cpu, latency, now)
        checks.append(
            VerifyCheck(
                name="metric",
                ok=spike is None,
                detail=f"{db.name}: " + (spike.detail if spike else "CPU and read latency normal"),
            )
        )
    return VerifyResult(
        ok=bool(checks) and all(c.ok for c in checks),
        checks=checks,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
