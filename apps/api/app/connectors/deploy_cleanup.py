"""Cleanup of what deploys leave behind (`make cleanup-deploys`, docs/phase4-infra.md).

`find_aws` and `find_kind` list what Groundcrew deploys created and decide, for each resource,
whether it goes: only resources tagged project=groundcrew (label, for namespaces) and older
than the cutoff, and never anything pinned (groundcrew:keep=true on AWS, label
groundcrew/keep=true in Kubernetes). They only read. `apply_aws` and `apply_kind` delete
what was planned, in dependency order: ECS services, security groups, log groups, ECR
repositories, secrets; then gc-* namespaces. Task definitions are never deregistered (the
deploy role cannot, they cost nothing, and a rollback needs the old revisions); they only
serve as evidence of a run's age.

Security groups have no creation time in EC2, so one goes only when its run (its
groundcrew:deploy tag) is provably older than the cutoff, from another resource of the same
run (a service, log group, repository, secret, or task definition), and only once no network
interface uses it.

Every AWS call goes through the deploy session (AWS_DEPLOY_OPERATIONS). `read_only_copy` gives
a dry run a separate session limited to reads, so even a bug in this module cannot write.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError, WaiterError

from app.config import Settings
from app.connectors.aws import (
    AWS_DEPLOY_OPERATIONS,
    AWS_READ_OPERATIONS,
    AwsError,
    error_code,
    guard_session,
)
from app.connectors.deploy_infra import (
    DEPLOY_TAG,
    K8S_DEPLOY_LABEL,
    K8S_KEEP_LABEL,
    KEEP_TAG,
    KEEP_VALUE,
    PROJECT_TAG,
    PROJECT_VALUE,
)

SERVICE, SECURITY_GROUP, LOG_GROUP, REPOSITORY, SECRET, TASK_DEFINITION, NAMESPACE = (
    "ECS service",
    "security group",
    "log group",
    "ECR repository",
    "secret",
    "task definition",
    "namespace",
)
ORDER = [SERVICE, SECURITY_GROUP, LOG_GROUP, REPOSITORY, SECRET, TASK_DEFINITION, NAMESPACE]
SECRET_PREFIX = "groundcrew/"  # noqa: S105 (a name prefix, not a value)
# tag:GetResources types: what cleanup deletes, plus task definitions (age evidence only).
RESOURCE_TYPES = [
    "ecs:service",
    "ecs:task-definition",
    "ec2:security-group",
    "logs:log-group",
    "ecr:repository",
    "secretsmanager:secret",
]
SG_DETACH_TIMEOUT_SECONDS = 180
SECRET_GONE_TIMEOUT_SECONDS = 60
NAMESPACE_GONE_TIMEOUT_SECONDS = 120


@dataclass
class Resource:
    """One resource found, what cleanup decided for it, and (after apply) what happened."""

    kind: str
    name: str
    ident: str  # what the delete call takes: service name, group id, ARN, namespace name
    run_id: str = ""
    created: datetime | None = None
    delete: bool = False
    note: str = ""
    result: str = ""  # apply: PASS, FAIL, or SKIP (left alone after a fresh check)
    pinned: bool = False  # groundcrew:keep=true / groundcrew/keep=true: never deleted
    seconds: float = 0.0

    def age(self, now: datetime) -> str:
        return format_age(now - self.created) if self.created else "?"


def format_age(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{hours:.1f} h" if hours < 48 else f"{hours / 24:.1f} d"


def read_only_copy(session: boto3.Session) -> boto3.Session:
    """A separate session with the same (frozen) credentials that may only read: deploy
    operations that are also read operations. The deploy session itself is cached and shared,
    so it is never re-guarded in place."""
    credentials = session.get_credentials()
    if credentials is None:
        raise AwsError("the deploy session has no credentials")
    frozen = credentials.get_frozen_credentials()
    copy = boto3.Session(
        aws_access_key_id=frozen.access_key,
        aws_secret_access_key=frozen.secret_key,
        aws_session_token=frozen.token,
        region_name=session.region_name,
    )
    return guard_session(copy, AWS_DEPLOY_OPERATIONS & AWS_READ_OPERATIONS)


def _tags(
    items: list[dict[str, str]] | None, key: str = "Key", value: str = "Value"
) -> dict[str, str]:
    return {t[key]: t[value] for t in items or []}


def _ours(tags: dict[str, str]) -> bool:
    return tags.get(PROJECT_TAG) == PROJECT_VALUE


def _pinned(tags: dict[str, str], key: str = KEEP_TAG) -> bool:
    return tags.get(key) == KEEP_VALUE


def _mark(r: Resource, tags: dict[str, str], key: str = KEEP_TAG) -> Resource:
    """Decide what the tags alone decide: not ours, or pinned. Returns r."""
    if not _ours(tags):
        r.note = "not tagged project=groundcrew"
    elif _pinned(tags, key):
        r.pinned, r.note = True, f"pinned ({key}={KEEP_VALUE})"
    return r


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ------------------------------------------------------------------------------------ find


def find_aws(
    session: boto3.Session, settings: Settings, *, older_than: timedelta, now: datetime
) -> list[Resource]:
    """Everything deploys created in this account, each with a decision. Reads only."""
    cutoff = now - older_than
    hours = format_age(older_than)
    tagged = _tagged(session)
    found = (
        _services(session, settings)
        + _log_groups(session, settings, tagged)
        + _repositories(session, settings, tagged)
        + _secrets(session, tagged)
    )
    for r in found:
        if r.note:  # already decided: not ours, pinned, or already being removed
            continue
        if r.created is None:
            r.note = "no creation time"
        elif r.created <= cutoff:
            r.delete, r.note = True, f"older than {hours}"
        else:
            r.note = f"younger than {hours}"
    groups = _security_groups(session)
    if groups:
        started = _run_started(session, found, tagged, {g.run_id for g in groups if g.run_id})
        for g in groups:
            if g.pinned:
                continue
            _decide_group(g, started, cutoff, hours)
            if g.delete and (count := attached_interfaces(session, g.ident)):
                g.note += f"; attached now to {count} network interface(s)"
    # Groundcrew's own families, listed so the output shows them; never deleted.
    kept = []
    for arn, tags in tagged.items():
        name = arn.rsplit("/", 1)[-1]
        if ":task-definition/" in arn and name.startswith("groundcrew-"):
            kept.append(Resource(TASK_DEFINITION, name, arn, run_id=tags.get(DEPLOY_TAG, ""),
                                 note="never deregistered"))  # fmt: skip
    return sorted(found + groups + kept, key=lambda r: (ORDER.index(r.kind), r.name))


def _tagged(session: boto3.Session) -> dict[str, dict[str, str]]:
    """ARN -> tags for everything tagged project=groundcrew (ARNs without a trailing ':*')."""
    pages = session.client("resourcegroupstaggingapi").get_paginator("get_resources")
    out: dict[str, dict[str, str]] = {}
    for page in pages.paginate(
        TagFilters=[{"Key": PROJECT_TAG, "Values": [PROJECT_VALUE]}],
        ResourceTypeFilters=RESOURCE_TYPES,
    ):
        for item in page.get("ResourceTagMappingList", []):
            out[str(item["ResourceARN"]).removesuffix(":*")] = _tags(item.get("Tags"))
    return out


def _services(session: boto3.Session, settings: Settings) -> list[Resource]:
    ecs = session.client("ecs")
    cluster = settings.gc_ecs_cluster
    arns: list[str] = []
    for page in ecs.get_paginator("list_services").paginate(cluster=cluster):
        arns += page.get("serviceArns", [])
    out = []
    for chunk in _chunks(arns, 10):
        described = ecs.describe_services(cluster=cluster, services=chunk, include=["TAGS"])
        for s in described.get("services", []):
            if s.get("status") == "INACTIVE":
                continue
            tags = _tags(s.get("tags"), "key", "value")
            r = Resource(SERVICE, f"{cluster}/{s['serviceName']}", str(s["serviceName"]),
                         run_id=tags.get(DEPLOY_TAG, ""), created=s.get("createdAt"))  # fmt: skip
            out.append(_mark(r, tags))
    return out


def _log_groups(
    session: boto3.Session, settings: Settings, tagged: dict[str, dict[str, str]]
) -> list[Resource]:
    pages = session.client("logs").get_paginator("describe_log_groups")
    out = []
    for page in pages.paginate(logGroupNamePrefix=settings.gc_log_group_prefix):
        for lg in page.get("logGroups", []):
            name = str(lg["logGroupName"])
            # The deploy role cannot read a log group's tags directly; tag:GetResources can.
            tags = tagged.get(str(lg.get("arn", "")).removesuffix(":*"), {})
            created = datetime.fromtimestamp(int(lg["creationTime"]) / 1000, UTC)
            r = Resource(LOG_GROUP, name, name, run_id=tags.get(DEPLOY_TAG, ""), created=created)
            out.append(_mark(r, tags))
    return out


def _repositories(
    session: boto3.Session, settings: Settings, tagged: dict[str, dict[str, str]]
) -> list[Resource]:
    """Found through tag:GetResources, then described by name: the deploy role may only
    describe repository/groundcrew-*, so listing every repository is refused."""
    ecr = session.client("ecr")
    out = []
    for arn, tags in sorted(tagged.items()):
        if ":ecr:" not in arn or ":repository/" not in arn:
            continue
        name = arn.split(":repository/", 1)[1]
        if not name.startswith(settings.gc_ecr_prefix):
            continue  # the deploy role could not delete it either
        try:
            repo = ecr.describe_repositories(repositoryNames=[name])["repositories"][0]
        except ClientError as exc:
            if error_code(exc) == "RepositoryNotFoundException":
                continue  # removed since tag:GetResources listed it
            raise
        r = Resource(REPOSITORY, name, name, run_id=tags.get(DEPLOY_TAG, ""),
                     created=repo.get("createdAt"))  # fmt: skip
        out.append(_mark(r, tags))
    return out


def _secrets(session: boto3.Session, tagged: dict[str, dict[str, str]]) -> list[Resource]:
    """Found through tag:GetResources (the deploy role has no ListSecrets), then described:
    DescribeSecret returns the name, tags, and dates, never the value."""
    sm = session.client("secretsmanager")
    out = []
    for arn in sorted(a for a in tagged if ":secretsmanager:" in a):
        if f":secret:{SECRET_PREFIX}" not in arn:
            continue  # the deploy role may only describe and delete groundcrew/*
        try:
            d = sm.describe_secret(SecretId=arn)
        except ClientError as exc:
            if error_code(exc) == "ResourceNotFoundException":
                continue  # removed since tag:GetResources listed it
            raise
        tags = _tags(d.get("Tags"))
        name = str(d["Name"])
        r = Resource(
            SECRET, name, arn, run_id=tags.get(DEPLOY_TAG, ""), created=d.get("CreatedDate")
        )
        _mark(r, tags)
        if not r.note and d.get("DeletedDate"):
            r.note = "already scheduled for deletion"
        out.append(r)
    return out


def _security_groups(session: boto3.Session) -> list[Resource]:
    """Only groups tagged project=groundcrew are ever looked at."""
    ec2 = session.client("ec2")
    pages = ec2.get_paginator("describe_security_groups").paginate(
        Filters=[{"Name": f"tag:{PROJECT_TAG}", "Values": [PROJECT_VALUE]}]
    )
    out = []
    for page in pages:
        for g in page.get("SecurityGroups", []):
            tags = _tags(g.get("Tags"))
            if not _ours(tags):
                continue
            gid = str(g["GroupId"])
            r = Resource(SECURITY_GROUP, f"{gid} ({g.get('GroupName', '')})", gid,
                         run_id=tags.get(DEPLOY_TAG, ""))  # fmt: skip
            out.append(_mark(r, tags))
    return out


def _run_started(
    session: boto3.Session,
    found: list[Resource],
    tagged: dict[str, dict[str, str]],
    runs: set[str],
) -> dict[str, tuple[datetime, str]]:
    """For each run: its earliest known creation time, and which resource proves it. Task
    definitions (never deleted) prove it when the run's other resources are already gone."""
    started: dict[str, tuple[datetime, str]] = {}

    def seen(run: str, when: datetime | None, what: str) -> None:
        if run in runs and when and (run not in started or when < started[run][0]):
            started[run] = (when, what)

    for r in found:
        if r.note != "not tagged project=groundcrew":
            seen(r.run_id, r.created, f"{r.kind} {r.name}")
    ecs = session.client("ecs")
    for arn, tags in sorted(tagged.items()):
        run = tags.get(DEPLOY_TAG, "")
        if ":task-definition/" not in arn or run not in runs or run in started:
            continue
        td = ecs.describe_task_definition(taskDefinition=arn)["taskDefinition"]
        seen(run, td.get("registeredAt"), f"task definition {arn.rsplit('/', 1)[-1]}")
    return started


def _decide_group(
    g: Resource,
    started: dict[str, tuple[datetime, str]],
    cutoff: datetime,
    hours: str,
) -> None:
    if not g.run_id:
        g.note = f"no {DEPLOY_TAG} tag, so its age cannot be proven; left alone"
        return
    if g.run_id not in started:
        g.note = f"age unknown: nothing else of run {g.run_id} is left to prove it; left alone"
        return
    when, evidence = started[g.run_id]
    g.created = when
    if when > cutoff:
        g.note = f"run younger than {hours} ({evidence})"
        return
    g.delete = True
    g.note = f"run older than {hours} ({evidence}); deleted only once unattached"


def attached_interfaces(session: boto3.Session, group_id: str) -> int:
    found = session.client("ec2").describe_network_interfaces(
        Filters=[{"Name": "group-id", "Values": [group_id]}]
    )
    return len(found.get("NetworkInterfaces", []))


# ----------------------------------------------------------------------------------- apply


def apply_aws(
    session: boto3.Session,
    settings: Settings,
    resources: list[Resource],
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Delete what find_aws planned, in dependency order. Each delete re-checks the resource
    first; a failure is recorded (result FAIL) and the rest still run."""
    deleters: dict[str, Callable[[boto3.Session, Settings, Resource], str]] = {
        SERVICE: _delete_service,
        SECURITY_GROUP: lambda s, _st, r: _delete_group(s, r, sleep, clock),
        LOG_GROUP: _delete_log_group,
        REPOSITORY: _delete_repository,
        SECRET: lambda s, _st, r: _delete_secret(s, r, sleep, clock),
    }
    for r in sorted(resources, key=lambda r: ORDER.index(r.kind)):
        if not r.delete or r.kind not in deleters:
            continue
        started = clock()
        try:
            r.note, r.result = deleters[r.kind](session, settings, r), "PASS"
        except LeftAloneError as exc:
            r.note, r.result = str(exc), "SKIP"
        except (ClientError, BotoCoreError, WaiterError, AwsError) as exc:
            r.note, r.result = f"{error_code(exc) or type(exc).__name__}: {exc}"[:200], "FAIL"
        r.seconds = clock() - started


class LeftAloneError(RuntimeError):
    """A fresh check found the resource is not ours, or not ready to delete."""


def _delete_service(session: boto3.Session, settings: Settings, r: Resource) -> str:
    ecs = session.client("ecs")
    cluster = settings.gc_ecs_cluster
    found = ecs.describe_services(cluster=cluster, services=[r.ident], include=["TAGS"])
    live = [s for s in found.get("services", []) if s.get("status") != "INACTIVE"]
    if not live:
        return "already gone"
    fresh = _tags(live[0].get("tags"), "key", "value")
    if not _ours(fresh) or _pinned(fresh):
        raise LeftAloneError("no longer tagged project=groundcrew, or now pinned")
    ecs.update_service(cluster=cluster, service=r.ident, desiredCount=0)
    ecs.delete_service(cluster=cluster, service=r.ident, force=True)
    ecs.get_waiter("services_inactive").wait(
        cluster=cluster, services=[r.ident], WaiterConfig={"Delay": 10, "MaxAttempts": 30}
    )
    return "scaled to 0, deleted, inactive"


def _delete_group(
    session: boto3.Session, r: Resource, sleep: Callable[[float], None], clock: Callable[[], float]
) -> str:
    """Only once no network interface uses it: a deleted service's task releases its
    interface a few minutes after the service is gone, so wait up to 3 minutes."""
    ec2 = session.client("ec2")
    groups = ec2.describe_security_groups(GroupIds=[r.ident])["SecurityGroups"]
    fresh = _tags(groups[0].get("Tags")) if groups else {}
    if not _ours(fresh) or _pinned(fresh):
        raise LeftAloneError("no longer tagged project=groundcrew, or now pinned")
    deadline = clock() + SG_DETACH_TIMEOUT_SECONDS
    while (count := attached_interfaces(session, r.ident)) > 0:
        if clock() > deadline:
            raise LeftAloneError(f"still attached to {count} network interface(s); left alone")
        sleep(10)
    try:
        ec2.delete_security_group(GroupId=r.ident)
    except ClientError as exc:
        if error_code(exc) == "DependencyViolation":
            raise LeftAloneError("still in use (DependencyViolation); left alone") from None
        raise
    return "unattached, deleted"


def _delete_log_group(session: boto3.Session, _settings: Settings, r: Resource) -> str:
    try:
        session.client("logs").delete_log_group(logGroupName=r.ident)
    except ClientError as exc:
        if error_code(exc) == "ResourceNotFoundException":
            return "already gone"
        raise
    return "deleted"


def _delete_repository(session: boto3.Session, _settings: Settings, r: Resource) -> str:
    try:
        session.client("ecr").delete_repository(repositoryName=r.ident, force=True)
    except ClientError as exc:
        if error_code(exc) == "RepositoryNotFoundException":
            return "already gone"
        raise
    return "deleted with its images (force)"


def _delete_secret(
    session: boto3.Session, r: Resource, sleep: Callable[[float], None], clock: Callable[[], float]
) -> str:
    sm = session.client("secretsmanager")
    try:
        d = sm.describe_secret(SecretId=r.ident)
    except ClientError as exc:
        if error_code(exc) == "ResourceNotFoundException":
            return "already gone"
        raise
    fresh = _tags(d.get("Tags"))
    if not _ours(fresh) or _pinned(fresh) or not str(d["Name"]).startswith(SECRET_PREFIX):
        raise LeftAloneError(
            "no longer a groundcrew/ secret tagged project=groundcrew, or now pinned"
        )
    sm.delete_secret(SecretId=r.ident, ForceDeleteWithoutRecovery=True)
    # Deletion is asynchronous: wait until it is really gone.
    deadline = clock() + SECRET_GONE_TIMEOUT_SECONDS
    while clock() < deadline:
        try:
            sm.describe_secret(SecretId=r.ident)
        except ClientError as exc:
            if error_code(exc) == "ResourceNotFoundException":
                return "force deleted (no recovery), gone"
            raise
        sleep(5)
    return "force deleted (no recovery); AWS is still removing it"


# ------------------------------------------------------------------------------------ kind


class Namespaces(Protocol):
    """The two namespace calls cleanup needs, as the groundcrew-deployer ServiceAccount."""

    def list(self) -> list[dict[str, Any]]:
        """Namespaces labelled project=groundcrew: name, labels, created, phase."""
        ...

    def delete(self, name: str) -> None: ...


class KubeNamespaces:
    """Namespaces through a kubeconfig (the deployer's, never cluster-admin)."""

    def __init__(self, kubeconfig: str) -> None:
        from kubernetes import client, config

        api = config.new_client_from_config(config_file=kubeconfig)
        self._core = client.CoreV1Api(api)

    def list(self) -> list[dict[str, Any]]:
        items = self._core.list_namespace(label_selector=f"{PROJECT_TAG}={PROJECT_VALUE}").items
        return [
            {
                "name": ns.metadata.name,
                "labels": ns.metadata.labels or {},
                "created": ns.metadata.creation_timestamp,
                "phase": ns.status.phase if ns.status else None,
            }
            for ns in items
        ]

    def delete(self, name: str) -> None:
        self._core.delete_namespace(name)


def find_kind(
    namespaces: Namespaces, settings: Settings, *, older_than: timedelta, now: datetime
) -> list[Resource]:
    """gc-* namespaces labelled project=groundcrew and older than the cutoff. Reads only."""
    cutoff = now - older_than
    out = []
    for ns in sorted(namespaces.list(), key=lambda n: str(n["name"])):
        name = str(ns["name"])
        labels = ns.get("labels") or {}
        r = Resource(NAMESPACE, name, name, run_id=labels.get(K8S_DEPLOY_LABEL, ""),
                     created=ns.get("created"))  # fmt: skip
        if labels.get(PROJECT_TAG) != PROJECT_VALUE:
            r.note = "not labelled project=groundcrew"
        elif not name.startswith(settings.gc_namespace_prefix):
            r.note = f"not {settings.gc_namespace_prefix}*"
        elif _pinned(labels, K8S_KEEP_LABEL):
            r.pinned, r.note = True, f"pinned ({K8S_KEEP_LABEL}={KEEP_VALUE})"
        elif ns.get("phase") == "Terminating":
            r.note = "already terminating"
        elif r.created is None or r.created > cutoff:
            r.note = f"younger than {format_age(older_than)}"
        else:
            r.delete, r.note = True, f"older than {format_age(older_than)}"
        out.append(r)
    return out


def apply_kind(
    namespaces: Namespaces,
    resources: list[Resource],
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    for r in resources:
        if not r.delete:
            continue
        started = clock()
        try:
            fresh = next((n for n in namespaces.list() if n["name"] == r.ident), None)
            if fresh is None:
                r.note, r.result = "already gone", "PASS"
                continue
            if _pinned(fresh.get("labels") or {}, K8S_KEEP_LABEL):
                r.note, r.result = "now pinned; left alone", "SKIP"
                continue
            namespaces.delete(r.ident)
            deadline = clock() + NAMESPACE_GONE_TIMEOUT_SECONDS
            while any(n["name"] == r.ident for n in namespaces.list()):
                if clock() > deadline:
                    r.note, r.result = "deleted; still terminating", "PASS"
                    break
                sleep(3)
            else:
                r.note, r.result = "deleted, gone", "PASS"
        except Exception as exc:  # the kubernetes client raises its own ApiException
            r.note, r.result = f"{type(exc).__name__}: {exc}"[:200], "FAIL"
        r.seconds = clock() - started
