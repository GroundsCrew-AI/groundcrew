"""ECS reader: service events, deployments and rollout state, and stopped tasks.

Stopped tasks carry the reasons that name most ECS incidents: `OutOfMemoryError: Container
killed due to memory usage` with exit code 137, `Task failed container health checks`, or an
operator's stop. ECS keeps stopped tasks for about an hour, so this is read live, not from the
last discovery.
"""

from datetime import datetime
from typing import Any

from app.agents.medic.events import ChangeMarker
from app.connectors.aws import AwsReader
from app.telemetry.evidence import Line, ReaderOutput, line, parse_time

SOURCE = "ecs"
MAX_STOPPED_TASKS = 50


def _short(arn: str | None) -> str:
    return str(arn or "?").rsplit("/", 1)[-1]


def _in(at: datetime | None, since: datetime, until: datetime) -> bool:
    return at is not None and since <= at <= until


def _task_lines(task: dict[str, Any], service: str) -> list[Line]:
    at = parse_time(task.get("stoppedAt")) or parse_time(task.get("stoppingAt"))
    if at is None:
        return []
    parts = [f"task {_short(task.get('taskArn'))[:12]} stopped: {task.get('stoppedReason', '?')}"]
    for container in task.get("containers", []):
        code, reason = container.get("exitCode"), container.get("reason")
        if code is None and not reason:
            continue
        detail = f"container {container.get('name')} exit code {code}"
        if reason:
            detail += f" ({reason})"
        if code == 137 or "OutOfMemory" in str(reason):
            detail += " [OOM]"
        parts.append(detail)
    parts.append(f"task definition {_short(task.get('taskDefinitionArn'))}")
    return [line(at, SOURCE, service, "; ".join(parts))]


def describe(
    service: dict[str, Any],
    tasks: list[dict[str, Any]],
    since: datetime,
    until: datetime,
) -> ReaderOutput:
    """Lines from a DescribeServices entry and DescribeTasks of its stopped tasks."""
    name = str(service.get("serviceName") or _short(service.get("serviceArn")))
    out = ReaderOutput()
    for event in service.get("events", []):
        at = parse_time(event.get("createdAt"))
        if _in(at, since, until) and at is not None:
            out.lines.append(line(at, SOURCE, name, str(event.get("message", ""))))
    for d in service.get("deployments", []):
        created, updated = parse_time(d.get("createdAt")), parse_time(d.get("updatedAt"))
        if not (
            _in(created, since, until) or _in(updated, since, until) or d.get("status") == "PRIMARY"
        ):
            continue
        text = (
            f"deployment {d.get('id', '?')} {d.get('status')} rollout {d.get('rolloutState')}"
            f" task definition {_short(d.get('taskDefinition'))}"
            f" running {d.get('runningCount')}/{d.get('desiredCount')}"
            f" failed tasks {d.get('failedTasks', 0)}"
        )
        if d.get("rolloutStateReason"):
            text += f": {d['rolloutStateReason']}"
        at = updated or created
        if at is not None:
            out.lines.append(line(max(min(at, until), since), SOURCE, name, text))
        if created is not None and _in(created, since, until):
            # The status line is stamped when the rollout last changed; a migration inside the
            # deploy breaks things from the start, before the new tasks are live.
            out.lines.append(
                line(
                    created,
                    SOURCE,
                    name,
                    f"deployment {d.get('id', '?')} started: task definition "
                    f"{_short(d.get('taskDefinition'))}",
                )
            )
            out.changes.append(
                ChangeMarker(
                    kind="deploy",
                    ref=str(d.get("taskDefinition") or d.get("id")),
                    at=created,
                    label=f"{name} deployment of {_short(d.get('taskDefinition'))}",
                )
            )
    running, desired = service.get("runningCount"), service.get("desiredCount")
    out.lines.append(
        line(
            until,
            SOURCE,
            name,
            f"now: {running}/{desired} tasks running, status {service.get('status')}",
        )
    )
    stopped = [t for t in tasks if _in(parse_time(t.get("stoppedAt")), since, until)]
    for task in stopped:
        out.lines.extend(_task_lines(task, name))
    out.truncated = len(tasks) >= MAX_STOPPED_TASKS
    return out


def read_service(
    reader: AwsReader,
    region: str | None,
    cluster: str,
    service_name: str,
    since: datetime,
    until: datetime,
) -> ReaderOutput:
    """Blocking (boto3): call with asyncio.to_thread."""
    found = reader.call(
        "ecs", region, "DescribeServices", cluster=cluster, services=[service_name]
    ).get("services", [])
    if not found:
        return ReaderOutput(lines=[line(until, SOURCE, service_name, "service not found")])
    arns = reader.paginate(
        "ecs",
        region,
        "ListTasks",
        "taskArns",
        limit=MAX_STOPPED_TASKS,
        cluster=cluster,
        serviceName=service_name,
        desiredStatus="STOPPED",
    )
    tasks: list[dict[str, Any]] = []
    for i in range(0, len(arns), 100):
        tasks.extend(
            reader.call(
                "ecs", region, "DescribeTasks", cluster=cluster, tasks=arns[i : i + 100]
            ).get("tasks", [])
        )
    return describe(found[0], tasks, since, until)
