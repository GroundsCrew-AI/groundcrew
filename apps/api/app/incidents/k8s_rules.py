"""Detector rules for Kubernetes Deployments and StatefulSets (app/incidents/rules.py has the
AWS ones).

- k8s_rollout_failed: a Deployment's rollout passed its progress deadline (Progressing=False,
  ProgressDeadlineExceeded), or a StatefulSet's rollout is stuck (currentRevision is not its
  updateRevision and its pods fail, k8s.STUCK_AFTER after the new revision appeared).
- k8s_pods_crashing: a pod is in CrashLoopBackOff, cannot pull its image or create its
  container, or exited non-zero (OOMKilled included) in the last 5 minutes.
- health_check: running pods (over a minute old) fail their readiness probe.
- log_error_spike: the pods' logs, by the same rule as CloudWatch log groups.

A signal is `down` when no pod of the workload is ready, so Medic may offer an emergency fix
without a failing health check.
"""

from datetime import datetime
from typing import Any

from app.agents.medic.events import Signal
from app.connectors.kubernetes import KubeReader
from app.incidents import rules
from app.telemetry import k8s
from app.telemetry.evidence import parse_time


def k8s_signals(resource_id: Any, workload: k8s.Workload, now: datetime) -> list[Signal]:
    name = workload.name
    ready, desired = k8s.readiness(workload.obj)
    state = f"{ready}/{desired} pods ready"
    down = desired > 0 and ready == 0
    out: list[Signal] = []

    failed = k8s.rollout_failed(workload.obj)
    if failed is not None:
        at = parse_time(failed.get("lastUpdateTime")) or now
        out.append(
            Signal(
                rule="k8s_rollout_failed",
                resource_id=resource_id,
                resource_name=name,
                detail=(
                    f"rollout failed: {failed.get('message') or 'progress deadline exceeded'} "
                    f"({state})"
                )[:500],
                at=min(at, now),
                down=down,
            )
        )

    problems = k8s.pod_problems(workload.pods, now, rules.RECENT)
    stuck = k8s.statefulset_stuck(workload, bool(problems), now)
    if stuck is not None:
        out.append(
            Signal(
                rule="k8s_rollout_failed",
                resource_id=resource_id,
                resource_name=name,
                detail=f"{stuck[0]} ({state})"[:500],
                at=min(stuck[1], now),
                down=down,
            )
        )
    crashing = [p for p in problems if p.crash]
    if crashing:
        reasons = sorted({p.reason for p in crashing})
        pods = sorted({p.pod for p in crashing})
        out.append(
            Signal(
                rule="k8s_pods_crashing",
                resource_id=resource_id,
                resource_name=name,
                detail=f"{len(pods)} pods crashing: {', '.join(reasons)}; {state}"[:500],
                at=min((p.at for p in crashing if p.at), default=now),
                down=down,
            )
        )
    unready = [p for p in problems if not p.crash]
    if unready:
        out.append(
            Signal(
                rule="health_check",
                resource_id=resource_id,
                resource_name=name,
                detail=f"{len(unready)} pods failing readiness checks ({state}, not healthy)",
                at=min((p.at for p in unready if p.at), default=now),
                down=down,
            )
        )
    return out


async def check_workload(
    reader: KubeReader,
    resource_id: Any,
    namespace: str,
    name: str,
    now: datetime,
    kind: str = k8s.DEPLOYMENT,
) -> list[Signal]:
    """Every rule for one Deployment or StatefulSet: its state, then its pods' logs."""
    workload = await k8s.fetch_workload(reader, namespace, name, kind)
    if workload is None:
        return []
    out = k8s_signals(resource_id, workload, now)
    window = rules.RECENT + rules.BASELINE
    logs = await k8s.fetch_logs(reader, workload, int(window.total_seconds()))
    spike = rules.log_signal(resource_id, name, logs, now)
    if spike is not None:
        out.append(spike)
    return out


async def check_deployment(
    reader: KubeReader, resource_id: Any, namespace: str, name: str, now: datetime
) -> list[Signal]:
    """Every rule for one Deployment."""
    return await check_workload(reader, resource_id, namespace, name, now, k8s.DEPLOYMENT)
