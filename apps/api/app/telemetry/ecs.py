"""ECS reader: service events, deployments and rollout state, stopped tasks, the health of the
running tasks and of the load balancer targets, and what each deploy in the window changed.

Stopped tasks carry the reasons that name most ECS incidents: `OutOfMemoryError: Container
killed due to memory usage` with exit code 137, `Task failed container health checks`, or an
operator's stop. ECS keeps stopped tasks for about an hour, so this is read live, not from the
last discovery.

"1/1 tasks running" says nothing about health, so the running tasks' health status and the
service's target groups (their health check and why each target fails it) are read too. A
deploy's task definition is compared with the one before it: same image and command means the
deploy changed no code, which the diagnosis must know before it blames the deploy. Environment
values are never shown, only the names that changed.
"""

from datetime import datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.agents.medic.events import ChangeMarker
from app.connectors.aws import AwsError, AwsReader, error_text
from app.telemetry.evidence import Line, ReaderOutput, line, parse_time

SOURCE = "ecs"
MAX_STOPPED_TASKS = 50
MAX_RUNNING_TASKS = 10
# Deploys in the window whose task definition is compared with the previous one.
MAX_COMPARED = 2
MAX_COMMAND_CHARS = 200
_READ_ERRORS = (AwsError, BotoCoreError, ClientError)


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
            f"now: {running}/{desired} tasks running, status {service.get('status')} "
            "(running is not healthy: see health checks and errors)",
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
    out = describe(found[0], tasks, since, until)
    out.lines += extras(reader, region, cluster, found[0], since, until)
    return out


def _revision(arn: str) -> tuple[str, int] | None:
    """("family", 3) from ...:task-definition/family:3."""
    family, _, rev = _short(arn).rpartition(":")
    return (family, int(rev)) if family and rev.isdigit() else None


def _previous(deployment: dict[str, Any], deployments: list[dict[str, Any]]) -> str | None:
    """The task definition a deploy replaced: the newest older deployment's, else revision - 1."""
    new = str(deployment.get("taskDefinition") or "")
    created = parse_time(deployment.get("createdAt"))
    older: list[tuple[datetime, str]] = []
    for d in deployments:
        at, td = parse_time(d.get("createdAt")), str(d.get("taskDefinition") or "")
        if created is not None and at is not None and at < created and td and td != new:
            older.append((at, td))
    if older:
        return max(older)[1]
    parsed = _revision(new)
    if parsed is None or parsed[1] <= 1:
        return None
    return f"{new.rsplit(':', 1)[0]}:{parsed[1] - 1}"


def _command(container: dict[str, Any]) -> str:
    text = " ".join(
        str(x) for x in [*(container.get("entryPoint") or []), *(container.get("command") or [])]
    )
    if len(text) > MAX_COMMAND_CHARS:
        text = text[: MAX_COMMAND_CHARS - 3] + "..."
    return text or "(image default)"


def _named(items: list[dict[str, Any]] | None, value: str) -> dict[str, str]:
    return {str(i.get("name")): str(i.get(value)) for i in items or [] if i.get("name")}


def _names(old: dict[str, str], new: dict[str, str], moved: str) -> list[str]:
    """Names added (+), removed (-), and whose value or source changed; never the values."""
    return [
        *(f"+{k}" for k in sorted(set(new) - set(old))),
        *(f"-{k}" for k in sorted(set(old) - set(new))),
        *(f"{k} {moved}" for k in sorted(set(old) & set(new)) if old[k] != new[k]),
    ]


def compare(new: dict[str, Any], old: dict[str, Any]) -> str:
    """What a task definition changed against the previous one, per container. Environment and
    secret values are never shown: only which names were added, removed, or changed."""
    old_by = {str(c.get("name")): c for c in old.get("containerDefinitions") or []}
    new_by = {str(c.get("name")): c for c in new.get("containerDefinitions") or []}
    parts: list[str] = []
    code = set(old_by) != set(new_by)
    for name, c in new_by.items():
        before = old_by.get(name)
        if before is None:
            parts.append(f"container {name} added (image {c.get('image')})")
            continue
        changed: list[str] = []
        if c.get("image") != before.get("image"):
            changed.append(f"image {before.get('image')} -> {c.get('image')}")
        if _command(c) != _command(before):
            changed.append(f"command {_command(before)!r} -> {_command(c)!r}")
        code = code or bool(changed)
        env = _names(
            _named(before.get("environment"), "value"),
            _named(c.get("environment"), "value"),
            "changed",
        )
        if env:
            changed.append(f"environment {', '.join(env)}")
        secrets = _names(
            _named(before.get("secrets"), "valueFrom"),
            _named(c.get("secrets"), "valueFrom"),
            "now from another secret",
        )
        if secrets:
            changed.append(f"secrets {', '.join(secrets)}")
        if (c.get("healthCheck") or {}).get("command") != (before.get("healthCheck") or {}).get(
            "command"
        ):
            changed.append("container health check changed")
        for field in ("cpu", "memory", "memoryReservation"):
            if c.get(field) != before.get(field):
                changed.append(f"{field} {before.get(field)} -> {c.get(field)}")
        detail = "; ".join(changed) if changed else "nothing changed"
        parts.append(f"container {name}: {detail}; runs {_command(c)!r}")
    for field in ("cpu", "memory"):
        if new.get(field) != old.get(field):
            parts.append(f"task {field} {old.get(field)} -> {new.get(field)}")
    parts += [f"container {n} removed" for n in sorted(set(old_by) - set(new_by))]
    head = (
        f"task definition {_short(new.get('taskDefinitionArn'))} vs "
        f"{_short(old.get('taskDefinitionArn'))}"
    )
    if not code:
        head += " (same images and commands: this deploy shipped no new application code)"
    return f"{head}: {' | '.join(parts) or 'no container changes'}"


def task_health(tasks: list[dict[str, Any]], service: str, now: datetime) -> list[Line]:
    """The running tasks' health status and each container's (UNKNOWN: no health check)."""
    out: list[Line] = []
    for task in tasks:
        if task.get("lastStatus") != "RUNNING":
            continue
        started = parse_time(task.get("startedAt"))
        age = f" for {int((now - started).total_seconds() // 60)} min" if started else ""
        text = (
            f"task {_short(task.get('taskArn'))[:12]} running{age}, health "
            f"{task.get('healthStatus') or 'UNKNOWN'}"
        )
        for c in task.get("containers") or []:
            status = c.get("healthStatus") or "UNKNOWN"
            text += f"; container {c.get('name')} health {status}"
            if status == "UNKNOWN":
                text += " (no container health check)"
        text += f"; task definition {_short(task.get('taskDefinitionArn'))}"
        out.append(line(now, SOURCE, service, text))
    return out


def target_health(
    groups: list[dict[str, Any]],
    health: dict[str, list[dict[str, Any]]],
    service: str,
    now: datetime,
) -> list[Line]:
    """Each target group's health check and how many of its targets pass it, and why not."""
    out: list[Line] = []
    for g in groups:
        arn = str(g.get("TargetGroupArn"))
        found = health.get(arn, [])
        bad = [h for h in found if (h.get("TargetHealth") or {}).get("State") != "healthy"]
        matcher = (g.get("Matcher") or {}).get("HttpCode") or "-"
        text = (
            f"load balancer target group {g.get('TargetGroupName')}: "
            f"{len(found) - len(bad)}/{len(found)} targets healthy (health check "
            f"{g.get('HealthCheckProtocol') or '-'} {g.get('HealthCheckPath') or ''} on port "
            f"{g.get('HealthCheckPort') or '-'}, expects {matcher}, timeout "
            f"{g.get('HealthCheckTimeoutSeconds', '-')} s every "
            f"{g.get('HealthCheckIntervalSeconds', '-')} s)"
        )
        for h in bad[:5]:
            th = h.get("TargetHealth") or {}
            target = h.get("Target") or {}
            text += (
                f"; {target.get('Id')}:{target.get('Port')} {th.get('State')} "
                f"{th.get('Reason') or ''} {th.get('Description') or ''}".rstrip()
            )
        out.append(line(now, SOURCE, service, text))
    return out


def _running_tasks(
    reader: AwsReader, region: str | None, cluster: str, service_name: str
) -> list[dict[str, Any]]:
    arns = reader.paginate(
        "ecs",
        region,
        "ListTasks",
        "taskArns",
        limit=MAX_RUNNING_TASKS,
        cluster=cluster,
        serviceName=service_name,
        desiredStatus="RUNNING",
    )
    if not arns:
        return []
    found: list[dict[str, Any]] = reader.call(
        "ecs", region, "DescribeTasks", cluster=cluster, tasks=arns[:MAX_RUNNING_TASKS]
    ).get("tasks", [])
    return found


def _targets(
    reader: AwsReader, region: str | None, service: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    arns = sorted(
        {
            str(lb["targetGroupArn"])
            for lb in service.get("loadBalancers") or []
            if lb.get("targetGroupArn")
        }
    )
    if not arns:
        return [], {}
    groups: list[dict[str, Any]] = reader.call(
        "elbv2", region, "DescribeTargetGroups", TargetGroupArns=arns
    ).get("TargetGroups", [])
    health = {
        str(g.get("TargetGroupArn")): reader.call(
            "elbv2", region, "DescribeTargetHealth", TargetGroupArn=g.get("TargetGroupArn")
        ).get("TargetHealthDescriptions", [])
        for g in groups
    }
    return groups, health


def _definition(reader: AwsReader, region: str | None, arn: str) -> dict[str, Any]:
    found: dict[str, Any] = (
        reader.call("ecs", region, "DescribeTaskDefinition", taskDefinition=arn).get(
            "taskDefinition"
        )
        or {}
    )
    return found


def _deploy_diffs(
    reader: AwsReader,
    region: str | None,
    service: dict[str, Any],
    since: datetime,
    until: datetime,
) -> list[Line]:
    name = str(service.get("serviceName") or _short(service.get("serviceArn")))
    deployments = list(service.get("deployments") or [])
    started = sorted(
        (d for d in deployments if _in(parse_time(d.get("createdAt")), since, until)),
        key=lambda d: parse_time(d.get("createdAt")) or since,
        reverse=True,
    )
    out: list[Line] = []
    for d in started[:MAX_COMPARED]:
        new_arn, old_arn = str(d.get("taskDefinition") or ""), _previous(d, deployments)
        at = parse_time(d.get("createdAt")) or since
        if not new_arn or not old_arn:
            continue
        try:
            text = compare(
                _definition(reader, region, new_arn), _definition(reader, region, old_arn)
            )
        except _READ_ERRORS as exc:
            text = (
                f"task definition {_short(new_arn)} not compared with {_short(old_arn)}: "
                f"{error_text(exc)}"
            )
        out.append(line(at, SOURCE, name, text))
    return out


def extras(
    reader: AwsReader,
    region: str | None,
    cluster: str,
    service: dict[str, Any],
    since: datetime,
    until: datetime,
) -> list[Line]:
    """Health of the running tasks and load balancer targets, and what each deploy changed.
    Each part is best effort: a failed read becomes a line saying so, not a failed reader."""
    name = str(service.get("serviceName") or _short(service.get("serviceArn")))
    out: list[Line] = []
    try:
        out += task_health(_running_tasks(reader, region, cluster, name), name, until)
    except _READ_ERRORS as exc:
        out.append(line(until, SOURCE, name, f"running task health not read: {error_text(exc)}"))
    try:
        groups, health = _targets(reader, region, service)
        out += target_health(groups, health, name, until)
    except _READ_ERRORS as exc:
        out.append(
            line(until, SOURCE, name, f"load balancer target health not read: {error_text(exc)}")
        )
    out += _deploy_diffs(reader, region, service, since, until)
    return out
