"""Detector rules for Kubernetes Deployments (app/incidents/rules.py has the AWS ones).

- k8s_rollout_failed: the rollout passed its progress deadline (Progressing=False,
  ProgressDeadlineExceeded).
- k8s_pods_crashing: a pod is in CrashLoopBackOff, cannot pull its image or create its
  container, or exited non-zero (OOMKilled included) in the last 5 minutes.
- health_check: running pods (over a minute old) fail their readiness probe.
- log_error_spike: the pods' logs, by the same rule as CloudWatch log groups.

A signal is `down` when no pod of the Deployment is ready, so Medic may offer an emergency fix
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
    ready, desired = k8s.readiness(workload.deployment)
    state = f"{ready}/{desired} pods ready"
    down = desired > 0 and ready == 0
    out: list[Signal] = []

    failed = k8s.rollout_failed(workload.deployment)
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


async def check_deployment(
    reader: KubeReader, resource_id: Any, namespace: str, name: str, now: datetime
) -> list[Signal]:
    """Every rule for one Deployment: its state, then its pods' logs."""
    workload = await k8s.fetch_workload(reader, namespace, name)
    if workload is None:
        return []
    out = k8s_signals(resource_id, workload, now)
    window = rules.RECENT + rules.BASELINE
    logs = await k8s.fetch_logs(reader, workload, int(window.total_seconds()))
    spike = rules.log_signal(resource_id, name, logs, now)
    if spike is not None:
        out.append(spike)
    return out
