"""app/connectors/deploy_cleanup.py and scripts/cleanup_deploys.py offline: what is found and
decided (only project=groundcrew, only older than the cutoff, security groups only when their
run is provably old), that the dry run only reads, and that apply deletes in order, never
deregisters a task definition, and leaves alone what a fresh check says is not ours."""

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest

from app.config import Settings
from app.connectors import deploy_cleanup as dc
from app.connectors.aws import AWS_DEPLOY_OPERATIONS, ReadOnlyViolationError
from tests.aws_fixture import ACCOUNT, REGION, fixture_session

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(hours=48)
NEW = NOW - timedelta(hours=1)
HOURS = timedelta(hours=24)
KEEP = {"groundcrew:keep": "true"}
CLUSTER = "groundcrew-deploys"


def _settings() -> Settings:
    return Settings(_env_file=None, gc_aws_region=REGION)


def _arn(service: str, resource: str) -> str:
    return f"arn:aws:{service}:{REGION}:{ACCOUNT}:{resource}"


def _tags(run: str | None) -> dict[str, str]:
    return {"project": "groundcrew", **({"groundcrew:deploy": run} if run else {})}


def _not_found(code: str) -> dict[str, Any]:
    return {"Error": {"Code": code, "Message": "not found"}}


class World:
    """A fake account. Every case cleanup must handle, and state that apply changes."""

    def __init__(self) -> None:
        self.services: dict[str, dict[str, Any]] = {
            "shop": {"createdAt": OLD, "tags": _tags("run-a"), "status": "ACTIVE"},
            "fresh": {"createdAt": NEW, "tags": _tags("run-b"), "status": "ACTIVE"},
            "legacy": {"createdAt": OLD, "tags": {}, "status": "ACTIVE"},
            "pinned": {"createdAt": OLD, "tags": {**_tags("run-p"), **KEEP}, "status": "ACTIVE"},
        }
        self.log_groups: dict[str, tuple[datetime, dict[str, str]]] = {
            "/groundcrew/shop": (OLD, _tags("run-a")),
            "/groundcrew/fresh": (NEW, _tags("run-b")),
            "/groundcrew/manual": (OLD, {}),
        }
        self.repos: dict[str, tuple[datetime, dict[str, str]]] = {
            "groundcrew-shop": (OLD, _tags("run-a")),
            "groundcrew-manual": (OLD, {}),
            "checkout-api": (OLD, {}),
        }
        self.secrets: dict[str, dict[str, Any]] = {
            _arn("secretsmanager", "secret:groundcrew/shop/DB_URL-AbCdEf"): {
                "Name": "groundcrew/shop/DB_URL", "CreatedDate": OLD,
                "Tags": [{"Key": k, "Value": v} for k, v in _tags("run-a").items()],
            },
            _arn("secretsmanager", "secret:checkout/DB_URL-AbCdEf"): {
                "Name": "checkout/DB_URL", "CreatedDate": OLD,
                "Tags": [{"Key": k, "Value": v} for k, v in _tags("run-z").items()],
            },
            _arn("secretsmanager", "secret:groundcrew/old/X-AbCdEf"): {
                "Name": "groundcrew/old/X", "CreatedDate": OLD, "DeletedDate": NEW,
                "Tags": [{"Key": k, "Value": v} for k, v in _tags("run-z").items()],
            },
        }  # fmt: skip
        self.groups: dict[str, dict[str, str]] = {
            "sg-a": _tags("run-a"),  # run proven old by its service
            "sg-b": _tags("run-b"),  # run younger than the cutoff
            "sg-c": _tags("run-c"),  # only a task definition is left to prove its age
            "sg-d": _tags("run-d"),  # nothing proves its age
            "sg-e": _tags(None),  # no run tag at all
            "sg-p": {**_tags("run-a"), **KEEP},  # its run is old, but it is pinned
        }
        self.task_definitions = {
            _arn("ecs", "task-definition/groundcrew-c:1"): (OLD, _tags("run-c")),
            _arn("ecs", "task-definition/groundcrew-shop:1"): (OLD, _tags("run-a")),
            # The demo's, tagged project=groundcrew too: not listed, never touched.
            _arn("ecs", "task-definition/checkout-api:9"): (OLD, _tags(None)),
        }
        self.attached: dict[str, int] = {"sg-a": 0}
        self.calls: list[str] = []

    def tagged(self) -> list[dict[str, Any]]:
        out = [(_arn("logs", f"log-group:{n}"), t) for n, (_, t) in self.log_groups.items()] + [
            (_arn("ecr", f"repository/{n}"), t) for n, (_, t) in self.repos.items()
        ]
        out += [(arn, {t["Key"]: t["Value"] for t in s["Tags"]}) for arn, s in self.secrets.items()]
        out += [(arn, t) for arn, (_, t) in self.task_definitions.items()]
        return [
            {"ResourceARN": arn, "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}
            for arn, tags in out
            if tags.get("project") == "groundcrew"
        ]

    def describe_services(self, p: dict[str, Any]) -> dict[str, Any]:
        found = []
        for ref in p["services"]:
            name = ref.rsplit("/", 1)[-1]
            if name in self.services:
                s = self.services[name]
                tags = [{"key": k, "value": v} for k, v in s["tags"].items()]
                found.append({"serviceName": name, "status": s["status"],
                              "createdAt": s["createdAt"], "tags": tags})  # fmt: skip
        return {"services": found, "failures": []}

    def describe_repositories(self, p: dict[str, Any]) -> dict[str, Any]:
        names = p.get("repositoryNames")
        # IAM: the deploy role may only describe repository/groundcrew-*, so a call without
        # names (authorized against repository/*) is refused, as on real AWS.
        if not names or not all(n.startswith("groundcrew-") for n in names):
            return {"Error": {"Code": "AccessDeniedException", "Message": "repository/*"}}
        if any(n not in self.repos for n in names):
            return _not_found("RepositoryNotFoundException")
        return {"repositories": [
            {"repositoryName": n, "repositoryArn": _arn("ecr", f"repository/{n}"),
             "createdAt": self.repos[n][0]}
            for n in names
        ]}  # fmt: skip

    def describe_secret(self, p: dict[str, Any]) -> dict[str, Any]:
        if ":secret:groundcrew/" not in p["SecretId"]:  # IAM: secret:groundcrew/* only
            return {"Error": {"Code": "AccessDeniedException", "Message": "not groundcrew/"}}
        s = self.secrets.get(p["SecretId"])
        return copy.deepcopy(s) if s else _not_found("ResourceNotFoundException")

    def describe_groups(self, p: dict[str, Any]) -> dict[str, Any]:
        ids = p.get("GroupIds") or list(self.groups)
        return {"SecurityGroups": [
            {"GroupId": g, "GroupName": f"name-{g}",
             "Tags": [{"Key": k, "Value": v} for k, v in self.groups[g].items()]}
            for g in ids if g in self.groups
        ]}  # fmt: skip

    def interfaces(self, p: dict[str, Any]) -> dict[str, Any]:
        gid = p["Filters"][0]["Values"][0]
        count = self.attached.get(gid, 0)
        if count:
            self.attached[gid] = count - 1  # released one poll later
        return {
            "NetworkInterfaces": [{"NetworkInterfaceId": f"eni-{i}"} for i in range(min(count, 3))]
        }

    def responses(self) -> dict[str, Any]:
        def deleted(table: dict[str, Any], key: str) -> Any:
            def respond(p: dict[str, Any]) -> dict[str, Any]:
                table.pop(p[key], None)
                return {}

            return respond

        def delete_service(p: dict[str, Any]) -> dict[str, Any]:
            self.services[p["service"]]["status"] = "INACTIVE"
            return {}

        return {
            "sts:GetCallerIdentity": {"Account": ACCOUNT, "Arn": "x", "UserId": "x"},
            "resourcegroupstaggingapi:GetResources": lambda _p: {
                "ResourceTagMappingList": self.tagged()
            },
            "ecs:ListServices": lambda _p: {
                "serviceArns": [_arn("ecs", f"service/{CLUSTER}/{n}") for n in self.services]
            },
            "ecs:DescribeServices": self.describe_services,
            "ecs:DescribeTaskDefinition": lambda p: {
                "taskDefinition": {"registeredAt": self.task_definitions[p["taskDefinition"]][0]}
            },
            "logs:DescribeLogGroups": lambda _p: {"logGroups": [
                {"logGroupName": n, "arn": _arn("logs", f"log-group:{n}:*"),
                 "creationTime": int(c.timestamp() * 1000)}
                for n, (c, _) in self.log_groups.items()
            ]},
            "ecr:DescribeRepositories": self.describe_repositories,
            "secretsmanager:DescribeSecret": self.describe_secret,
            "ec2:DescribeSecurityGroups": self.describe_groups,
            "ec2:DescribeNetworkInterfaces": self.interfaces,
            "ecs:UpdateService": {},
            "ecs:DeleteService": delete_service,
            "ec2:DeleteSecurityGroup": deleted(self.groups, "GroupId"),
            "logs:DeleteLogGroup": deleted(self.log_groups, "logGroupName"),
            "ecr:DeleteRepository": deleted(self.repos, "repositoryName"),
            "secretsmanager:DeleteSecret": deleted(self.secrets, "SecretId"),
        }  # fmt: skip

    def session(self, allowed: frozenset[str] | None = None) -> boto3.Session:
        if allowed is None:
            return fixture_session(self.responses(), self.calls)
        return fixture_session(self.responses(), self.calls, allowed)


def _by_name(found: list[dc.Resource]) -> dict[str, dc.Resource]:
    return {r.name: r for r in found}


def test_dry_run_only_reads() -> None:
    world = World()
    # The read-only allowlist: any write raises.
    found = dc.find_aws(world.session(), _settings(), older_than=HOURS, now=NOW)
    assert found
    assert all(c.split(":", 1)[1].startswith(("Describe", "Get", "List")) for c in world.calls)


def test_only_old_project_tagged_resources_are_planned() -> None:
    world = World()
    r = _by_name(dc.find_aws(world.session(), _settings(), older_than=HOURS, now=NOW))
    planned = {name for name, res in r.items() if res.delete}
    assert planned == {
        f"{CLUSTER}/shop",
        "/groundcrew/shop",
        "groundcrew-shop",
        "groundcrew/shop/DB_URL",
        "sg-a (name-sg-a)",
        "sg-c (name-sg-c)",
    }
    assert r[f"{CLUSTER}/fresh"].note == "younger than 24.0 h"
    assert r[f"{CLUSTER}/legacy"].note == "not tagged project=groundcrew"
    assert r["/groundcrew/manual"].note == "not tagged project=groundcrew"
    # Repositories are found by tag: untagged ones are never even described.
    assert "groundcrew-manual" not in r and "checkout-api" not in r
    assert r["groundcrew/old/X"].note == "already scheduled for deletion"
    for pinned in (f"{CLUSTER}/pinned", "sg-p (name-sg-p)"):
        assert r[pinned].pinned and not r[pinned].delete
        assert r[pinned].note == "pinned (groundcrew:keep=true)"
    assert "checkout/DB_URL" not in r  # tagged, but outside groundcrew/: never described


def test_security_groups_go_only_when_their_run_is_provably_old() -> None:
    world = World()
    r = _by_name(dc.find_aws(world.session(), _settings(), older_than=HOURS, now=NOW))
    assert f"ECS service {CLUSTER}/shop" in r["sg-a (name-sg-a)"].note
    assert "task definition groundcrew-c:1" in r["sg-c (name-sg-c)"].note
    assert r["sg-b (name-sg-b)"].delete is False and "run younger" in r["sg-b (name-sg-b)"].note
    assert r["sg-d (name-sg-d)"].delete is False and "age unknown" in r["sg-d (name-sg-d)"].note
    assert (
        r["sg-e (name-sg-e)"].delete is False and "cannot be proven" in r["sg-e (name-sg-e)"].note
    )
    # Task definitions are only evidence: described for run-c alone, never planned.
    assert world.calls.count("ecs:DescribeTaskDefinition") == 1
    tds = {n: res for n, res in r.items() if res.kind == dc.TASK_DEFINITION}
    assert set(tds) == {"groundcrew-c:1", "groundcrew-shop:1"}
    assert all(not res.delete and res.note == "never deregistered" for res in tds.values())


def test_a_planned_group_shows_it_is_still_attached() -> None:
    world = World()
    world.attached["sg-a"] = 2
    r = _by_name(dc.find_aws(world.session(), _settings(), older_than=HOURS, now=NOW))
    assert "attached now to 2 network interface(s)" in r["sg-a (name-sg-a)"].note


def test_only_tagged_security_groups_are_asked_for() -> None:
    world = World()
    seen: list[dict[str, Any]] = []
    responses = world.responses()
    original = responses["ec2:DescribeSecurityGroups"]

    def describe(p: dict[str, Any]) -> Any:
        seen.append(p)
        return original(p)

    responses["ec2:DescribeSecurityGroups"] = describe
    dc.find_aws(fixture_session(responses), _settings(), older_than=HOURS, now=NOW)
    assert seen[0]["Filters"] == [{"Name": "tag:project", "Values": ["groundcrew"]}]


def test_apply_deletes_in_order_and_never_deregisters() -> None:
    world = World()
    settings = _settings()
    found = dc.find_aws(world.session(), settings, older_than=HOURS, now=NOW)
    world.attached["sg-a"] = 2  # the deleted service's task is still releasing its interface
    world.calls.clear()
    sleeps: list[float] = []
    dc.apply_aws(world.session(AWS_DEPLOY_OPERATIONS), settings, found, sleep=sleeps.append)
    deletes = [c for c in world.calls if ":Delete" in c]
    assert deletes == [
        "ecs:DeleteService",
        "ec2:DeleteSecurityGroup",
        "ec2:DeleteSecurityGroup",
        "logs:DeleteLogGroup",
        "ecr:DeleteRepository",
        "secretsmanager:DeleteSecret",
    ]
    assert "ecs:DeregisterTaskDefinition" not in world.calls
    assert sleeps == [10, 10]  # waited for sg-a to be unattached
    assert set(world.groups) == {"sg-b", "sg-d", "sg-e", "sg-p"}  # sg-p is pinned
    assert set(world.log_groups) == {"/groundcrew/fresh", "/groundcrew/manual"}
    assert set(world.repos) == {"groundcrew-manual", "checkout-api"}
    for kept in ("fresh", "legacy", "pinned"):
        assert world.services[kept]["status"] == "ACTIVE"
    secret = next(r for r in found if r.name == "groundcrew/shop/DB_URL")
    assert secret.note == "force deleted (no recovery), gone"
    assert all(r.result == "PASS" for r in found if r.delete)


def test_apply_leaves_alone_what_a_fresh_check_says_is_not_ours() -> None:
    world = World()
    settings = _settings()
    found = dc.find_aws(world.session(), settings, older_than=HOURS, now=NOW)
    world.services["shop"]["tags"] = {**_tags("run-a"), **KEEP}  # pinned since the plan
    world.attached["sg-a"] = 10**6  # never released
    clock = iter(range(0, 10_000, 30))
    dc.apply_aws(world.session(AWS_DEPLOY_OPERATIONS), settings, found,
                 sleep=lambda _s: None, clock=lambda: float(next(clock)))  # fmt: skip
    r = _by_name(found)
    assert r[f"{CLUSTER}/shop"].result == "SKIP"
    assert r["sg-a (name-sg-a)"].result == "SKIP" and "still attached" in r["sg-a (name-sg-a)"].note
    assert "ecs:DeleteService" not in world.calls
    assert world.calls.count("ec2:DeleteSecurityGroup") == 1  # sg-c only
    assert r["/groundcrew/shop"].result == "PASS"  # the rest still ran


def test_a_failed_delete_does_not_stop_the_rest() -> None:
    world = World()
    settings = _settings()
    found = dc.find_aws(world.session(), settings, older_than=HOURS, now=NOW)
    responses = world.responses()
    responses["logs:DeleteLogGroup"] = {"Error": {"Code": "AccessDeniedException", "Message": "no"}}
    session = fixture_session(responses, world.calls, AWS_DEPLOY_OPERATIONS)
    dc.apply_aws(session, settings, found, sleep=lambda _s: None)
    r = _by_name(found)
    assert r["/groundcrew/shop"].result == "FAIL"
    assert r["/groundcrew/shop"].note.startswith("AccessDeniedException")
    assert r["groundcrew-shop"].result == "PASS" and r["groundcrew/shop/DB_URL"].result == "PASS"


def test_read_only_copy_refuses_writes() -> None:
    world = World()
    reader = dc.read_only_copy(world.session(AWS_DEPLOY_OPERATIONS))
    reader.events.register("before-call", lambda **_: (None, {}))  # never reaches AWS
    with pytest.raises(ReadOnlyViolationError):
        reader.client("logs", region_name=REGION).delete_log_group(logGroupName="/groundcrew/x")
    with pytest.raises(ReadOnlyViolationError):
        reader.client("ecs", region_name=REGION).deregister_task_definition(taskDefinition="x:1")


# --- kind --------------------------------------------------------------------------------


class FakeNamespaces:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = [
            {"name": "groundcrew-system", "labels": {"project": "groundcrew"}, "created": OLD},
            {"name": "gc-shop", "labels": {**_tags(None), "groundcrew/deploy": "run-a"},
             "created": OLD},
            {"name": "gc-fresh", "labels": _tags(None), "created": NEW},
            {"name": "gc-going", "labels": _tags(None), "created": OLD, "phase": "Terminating"},
            {"name": "gc-whoami", "labels": {**_tags(None), "groundcrew/keep": "true"},
             "created": OLD},
        ]  # fmt: skip
        self.deleted: list[str] = []

    def list(self) -> list[dict[str, Any]]:
        return [n for n in self.items if n["name"] not in self.deleted]

    def delete(self, name: str) -> None:
        self.deleted.append(name)


def test_kind_deletes_only_old_gc_namespaces() -> None:
    ns = FakeNamespaces()
    found = dc.find_kind(ns, _settings(), older_than=HOURS, now=NOW)
    r = _by_name(found)
    assert [n for n, res in r.items() if res.delete] == ["gc-shop"]
    assert r["groundcrew-system"].note == "not gc-*"
    assert r["gc-fresh"].note == "younger than 24.0 h"
    assert r["gc-going"].note == "already terminating"
    assert r["gc-whoami"].pinned and r["gc-whoami"].note == "pinned (groundcrew/keep=true)"
    assert r["gc-shop"].run_id == "run-a"
    assert ns.deleted == []  # find only reads
    dc.apply_kind(ns, found, sleep=lambda _s: None)
    assert ns.deleted == ["gc-shop"] and r["gc-shop"].result == "PASS"


def test_a_namespace_pinned_after_the_plan_is_left_alone() -> None:
    ns = FakeNamespaces()
    found = dc.find_kind(ns, _settings(), older_than=HOURS, now=NOW)
    ns.items[1]["labels"]["groundcrew/keep"] = "true"  # gc-shop, pinned since the plan
    dc.apply_kind(ns, found, sleep=lambda _s: None)
    shop = _by_name(found)["gc-shop"]
    assert ns.deleted == [] and shop.result == "SKIP" and "pinned" in shop.note


def _script() -> Any:
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "scripts" / "cleanup_deploys.py"
    spec = importlib.util.spec_from_file_location("cleanup_deploys", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["cleanup_deploys"] = module
    spec.loader.exec_module(module)
    return module


def test_the_output_shows_pinned_resources() -> None:
    module = _script()
    found = dc.find_kind(FakeNamespaces(), _settings(), older_than=HOURS, now=NOW)
    lines = module.render([module.Target("kind", found)], NOW)
    whoami = next(line for line in lines if "gc-whoami" in line)
    assert whoami.split()[:2] == ["keep", "(pinned)"]


def test_the_wrong_platform_account_stops_before_any_role_is_assumed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import os

    monkeypatch.chdir(os.getcwd())  # main() changes directory; restored after the test
    module = _script()
    monkeypatch.setattr(module, "platform_account", lambda _settings: "111122223333")

    def no_sessions(*_args: Any) -> Any:
        raise AssertionError("assumed a role in the wrong account")

    monkeypatch.setattr(module, "_deploy_sessions", no_sessions)
    monkeypatch.setattr(module, "get_settings", _settings)
    assert module.main(["--hours", "1", "--account", ACCOUNT]) == 1
    assert f"account 111122223333, not {ACCOUNT}" in capsys.readouterr().out


def test_run_limits_deletes_to_that_runs_resources() -> None:
    module = _script()
    mine = dc.Resource("log group", "/groundcrew/a", "/groundcrew/a", run_id="run-1", delete=True)
    other = dc.Resource("log group", "/groundcrew/b", "/groundcrew/b", run_id="run-2", delete=True)
    untagged = dc.Resource("log group", "/groundcrew/c", "/groundcrew/c", delete=True)
    module.only_run([mine, other, untagged], "run-1")
    assert (mine.delete, other.delete, untagged.delete) == (True, False, False)
    assert other.note == "another run (run-2)" and untagged.note == "another run (untagged)"
    everything = dc.Resource("log group", "/groundcrew/d", "/groundcrew/d", run_id="x", delete=True)
    module.only_run([everything], None)
    assert everything.delete
