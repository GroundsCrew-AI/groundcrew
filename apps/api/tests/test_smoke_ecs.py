"""scripts/smoke_ecs_deploy.py offline: the dry run only reads, the five negative checks and
their verdicts, cleanup touches only what is ours, and the dummy secret never leaves the
process (output, report). The real run is `make smoke-ecs DRY_RUN=0` (docs/deploy-live.md)."""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.config import Settings
from tests.aws_fixture import ACCOUNT, REGION, fixture_session

ROOT = Path(__file__).resolve().parents[3]
RUN = "smoke-20261009T120000Z"
SLR = "role/aws-service-role/ecs.amazonaws.com/AWSServiceRoleForECS"
SECRET_ARN = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:groundcrew/smoke/TEST_VAR-AbCdEf"
TASK_DEF_ARN = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/groundcrew-smoke:1"
DEPLOY_ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/groundcrew-deploy/groundcrew-deploy"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "smoke_ecs_deploy", ROOT / "scripts" / "smoke_ecs_deploy.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["smoke_ecs_deploy"] = module
    spec.loader.exec_module(module)
    return module


smoke = _load()


def _settings() -> Settings:
    return Settings(_env_file=None, gc_aws_region=REGION)


def _not_found(code: str) -> dict[str, Any]:
    return {"Error": {"Code": code, "Message": "not found"}}


# Read-only answers for the dry run: the deploy session and the read-only session.
DRY = {
    "sts:GetCallerIdentity": {"Account": ACCOUNT, "Arn": DEPLOY_ARN, "UserId": "x"},
    "ecs:DescribeClusters": {
        "clusters": [
            {
                "clusterArn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/groundcrew-deploys",
                "status": "ACTIVE",
            }
        ]
    },
    "ec2:DescribeVpcs": {"Vpcs": [{"VpcId": "vpc-0default", "IsDefault": True}]},
    "ec2:DescribeSubnets": {"Subnets": [{"SubnetId": "subnet-0a", "VpcId": "vpc-0default"}]},
    "iam:GetRole": {
        "Role": {
            "Path": "/aws-service-role/ecs.amazonaws.com/",
            "RoleName": "AWSServiceRoleForECS",
            "RoleId": "AROAEXAMPLE0000000000",
            "Arn": f"arn:aws:iam::{ACCOUNT}:{SLR}",
            "CreateDate": "2026-01-01T00:00:00Z",
        }
    },
    "ecr:DescribeRepositories": _not_found("RepositoryNotFoundException"),
    "logs:DescribeLogGroups": {"logGroups": []},
    "secretsmanager:DescribeSecret": _not_found("ResourceNotFoundException"),
    "ecs:DescribeServices": {"services": [], "failures": []},
    "ec2:DescribeSecurityGroups": {"SecurityGroups": []},
}


def _docker_ok(*args: str, **_: Any) -> Any:
    return None


def _smoke(deploy: Any, read: Any, out: list[str], **kwargs: Any) -> Any:
    return smoke.Smoke(
        deploy,
        read,
        _settings(),
        account=ACCOUNT,
        run_id=RUN,
        dry_run=kwargs.pop("dry_run", True),
        scrub=smoke.Scrubber(out.append),
        docker=kwargs.pop("docker", _docker_ok),
        public_ip=lambda: "203.0.113.7",
        http_get=kwargs.pop("http_get", lambda _url: 200),
    )


# --- dry run -----------------------------------------------------------------------------


def test_dry_run_only_reads_and_prints_the_plan() -> None:
    calls: list[str] = []
    # Both sessions are guarded by the READ-ONLY allowlist: any write would raise.
    deploy, read = fixture_session(DRY, calls), fixture_session(DRY, calls)
    out: list[str] = []
    s = _smoke(deploy, read, out)
    assert s.prechecks() is True, out
    assert all(c.split(":", 1)[1].startswith(("Describe", "Get", "List")) for c in calls), calls
    assert "iam:GetRole" in calls and "sts:GetCallerIdentity" in calls
    plan = "\n".join(s.plan())
    for name in (
        "groundcrew-smoke",
        "/groundcrew/smoke",
        "groundcrew/smoke/TEST_VAR",
        "vpc-0default",
    ):
        assert name in plan
    assert f"groundcrew:deploy={RUN}" in plan and "project=groundcrew" in plan
    assert "203.0.113.7/32" in "\n".join(out)
    assert all(step.result == "PASS" for step in s.report.steps)


def test_dry_run_stops_on_leftovers_and_the_wrong_identity() -> None:
    leftovers = {
        **DRY,
        "ecr:DescribeRepositories": {"repositories": [{"repositoryName": "groundcrew-smoke"}]},
        "sts:GetCallerIdentity": {
            "Account": ACCOUNT,
            "Arn": "arn:aws:iam::881050425883:user/me",
            "UserId": "x",
        },
    }
    out: list[str] = []
    s = _smoke(fixture_session(leftovers), fixture_session(leftovers), out)
    assert s.prechecks() is False
    failed = {step.name: step.note for step in s.report.steps if step.result == "FAIL"}
    assert "not the deploy role" in failed["identity is the deploy role"]
    assert "CLEANUP=1" in failed["no leftovers from an earlier run"]


# --- secrets never leave the process --------------------------------------------------------


def test_scrubber_refuses_to_output_a_registered_value(tmp_path: Path) -> None:
    out: list[str] = []
    scrub = smoke.Scrubber(out.append)
    scrub.register("dummy-secret-value-123")
    assert scrub.clean("x dummy-secret-value-123 y") == "x *** y"
    with pytest.raises(smoke.LeakError):
        scrub.say("leaked dummy-secret-value-123")
    report = smoke.Report(run_id=RUN, dry_run=False)
    report.steps.append(smoke.Step("bad", "FAIL", 0, "dummy-secret-value-123"))
    with pytest.raises(smoke.LeakError):
        smoke.write_report(report, scrub, tmp_path)
    assert out == []


# --- negative checks ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,outcome,verdict",
    [
        ("a GetSecretValue groundcrew/smoke/TEST_VAR", "AccessDeniedException", "PASS"),
        ("a GetSecretValue groundcrew/smoke/TEST_VAR", "allowed", "GAP"),
        ("b CreateRepository smoke-untagged-name", "AccessDeniedException", "PASS"),
        ("b CreateRepository smoke-untagged-name", "allowed", "GAP"),
        ("c CreateSecurityGroup without the project tag (DryRun)", "UnauthorizedOperation", "PASS"),
        ("c CreateSecurityGroup without the project tag (DryRun)", "DryRunOperation", "GAP"),
        ("d CreateService in smoke-nonexistent-cluster", "AccessDeniedException", "PASS"),
        ("d CreateService in smoke-nonexistent-cluster", "ClusterNotFoundException", "GAP"),
        ("e CreateRole groundcrew-smoke-should-not-exist", "AccessDenied", "PASS"),
        ("e CreateRole groundcrew-smoke-should-not-exist", "Throttling", "FAIL"),
    ],
)
def test_negative_verdicts(name: str, outcome: str, verdict: str) -> None:
    assert smoke.negative_verdict(name, outcome) == verdict


class _RawClient:
    def __init__(
        self, service: str, seen: list[tuple[str, str, dict[str, Any]]], answers: dict[str, str]
    ):
        self.service, self.seen, self.answers = service, seen, answers

    def __getattr__(self, op: str) -> Any:
        def call(**params: Any) -> Any:
            self.seen.append((self.service, op, params))
            code = self.answers[f"{self.service}.{op}"]
            if code == "allowed":
                return {"SecretString": "must-never-be-returned"}
            raise ClientError({"Error": {"Code": code, "Message": "no"}}, op)

        return call


def test_negative_checks_send_exactly_five_hardcoded_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str, dict[str, Any]]] = []
    answers = {
        "secretsmanager.get_secret_value": "allowed",
        "ecr.create_repository": "AccessDeniedException",
        "ec2.create_security_group": "UnauthorizedOperation",
        "ecs.create_service": "AccessDeniedException",
        "iam.create_role": "AccessDenied",
    }

    class RawSession:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["aws_access_key_id"] == "AKIADEPLOY" and kwargs["region_name"] == REGION

        def client(self, service: str) -> _RawClient:
            return _RawClient(service, seen, answers)

    class Creds:
        def get_frozen_credentials(self) -> Any:
            return type("F", (), {"access_key": "AKIADEPLOY", "secret_key": "s", "token": "t"})()

    class Deploy:
        def get_credentials(self) -> Creds:
            return Creds()

    monkeypatch.setattr(smoke.boto3, "Session", RawSession)
    tags = [{"Key": "project", "Value": "groundcrew"}, {"Key": "groundcrew:deploy", "Value": RUN}]
    results = smoke.negative_checks(Deploy(), REGION, "vpc-0default", "groundcrew-smoke:1", tags)
    assert [(s, op) for s, op, _ in seen] == [
        ("secretsmanager", "get_secret_value"),
        ("ecr", "create_repository"),
        ("ec2", "create_security_group"),
        ("ecs", "create_service"),
        ("iam", "create_role"),
    ]
    params = {op: p for _, op, p in seen}
    assert params["create_repository"]["repositoryName"] == "smoke-untagged-name"
    assert params["create_security_group"]["DryRun"] is True
    assert "TagSpecifications" not in params["create_security_group"]
    assert params["create_service"]["cluster"] == "smoke-nonexistent-cluster"
    # The wrongly allowed GetSecretValue is reported as "allowed", never with its body.
    assert results[0] == ("a GetSecretValue groundcrew/smoke/TEST_VAR", "allowed")
    assert "must-never-be-returned" not in json.dumps(results)


def test_only_negative_checks_build_an_unguarded_session() -> None:
    source = (ROOT / "scripts" / "smoke_ecs_deploy.py").read_text()
    body = source.split("def negative_checks(", 1)[1].split("\ndef ", 1)[0]
    assert source.count("boto3.Session(") == 1 and "boto3.Session(" in body
    assert "get_frozen_credentials" not in source.replace(body, "")


# --- the real run and cleanup on fakes ----------------------------------------------------


class _Fake:
    """A client whose operations are plain functions; records every call."""

    def __init__(self, name: str, log: list[str], ops: dict[str, Any]) -> None:
        self.name, self.log, self.ops = name, log, ops

    def __getattr__(self, op: str) -> Any:
        if op == "get_waiter":
            return lambda waiter: type(
                "W", (), {"wait": lambda _self, **kw: self.log.append(f"{self.name}.wait.{waiter}")}
            )()
        if op not in self.ops:
            raise AssertionError(f"unexpected call {self.name}.{op}")

        def call(**params: Any) -> Any:
            self.log.append(f"{self.name}.{op}")
            result = self.ops[op]
            return result(**params) if callable(result) else result

        return call


def _refused(**_: Any) -> Any:
    """What the deploy session's code allowlist does for GetSecretValue and CreateRole."""
    raise smoke.ReadOnlyViolationError("refused by the allowlist")


class _World:
    """Fake AWS for one smoke run: remembers the secret so the container 'logs' its hash."""

    def __init__(self, *, ours: bool = True) -> None:
        self.log: list[str] = []
        self.secret = ""
        self.ours = ours
        tags = [
            {"Key": "project", "Value": "groundcrew"},
            {"Key": "groundcrew:deploy", "Value": RUN if ours else "other"},
        ]
        self.tags = tags

    def _create_secret(self, **p: Any) -> Any:
        self.secret = p["SecretString"]
        return {"ARN": SECRET_ARN}

    def _logs(self, **_: Any) -> Any:
        digest = hashlib.sha256(self.secret.encode()).hexdigest()[:12]
        return {"events": [{"message": f"TEST_VAR=set sha256:{digest}"}]}

    def client(self, service: str, region_name: str | None = None) -> _Fake:
        ecs_tags = [{"key": t["Key"], "value": t["Value"]} for t in self.tags]
        token = "QVdTOnJlZ2lzdHJ5LXBhc3N3b3Jk"  # noqa: S105 (base64 of AWS:registry-password)
        ops: dict[str, dict[str, Any]] = {
            "ecr": {
                "create_repository": {},
                "get_authorization_token": {
                    "authorizationData": [
                        {
                            "authorizationToken": token,
                            "proxyEndpoint": f"https://{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com",
                        }
                    ]
                },
                "delete_repository": {},
            },
            "logs": {
                "create_log_group": {},
                "put_retention_policy": {},
                "filter_log_events": self._logs,
                "delete_log_group": {},
            },
            "ec2": {
                "create_security_group": {"GroupId": "sg-0smoke"},
                "authorize_security_group_ingress": {},
                "describe_network_interfaces": {
                    "NetworkInterfaces": [{"Association": {"PublicIp": "3.1.2.3"}}]
                },
                "describe_security_groups": {
                    "SecurityGroups": [
                        {
                            "GroupId": "sg-0smoke",
                            "GroupName": "groundcrew-smoke-x",
                            "Tags": self.tags,
                        },
                        {"GroupId": "sg-0someone", "GroupName": "groundcrew-smoke-y", "Tags": []},
                    ]
                },
                "delete_security_group": {},
            },
            "secretsmanager": {
                "get_secret_value": _refused,
                "create_secret": self._create_secret,
                "describe_secret": {"Tags": self.tags},
                "delete_secret": {},
            },
            "ecs": {
                "register_task_definition": {"taskDefinition": {"taskDefinitionArn": TASK_DEF_ARN}},
                "create_service": {},
                "list_tasks": {"taskArns": ["arn:task/1"]},
                "describe_tasks": {
                    "tasks": [
                        {
                            "taskArn": "arn:aws:ecs:x:task/groundcrew-deploys/abc",
                            "attachments": [
                                {"details": [{"name": "networkInterfaceId", "value": "eni-1"}]}
                            ],
                        }
                    ]
                },
                "describe_services": {"services": [{"status": "ACTIVE", "tags": ecs_tags}]},
                "update_service": {},
                "delete_service": {},
            },
        }
        ops["iam"] = {"create_role": _refused}
        return _Fake(service, self.log, ops.get(service, {}))

    def get_credentials(self) -> Any:
        raise AssertionError("the real-run test replaces negative_checks")


def test_real_run_deploys_checks_and_cleans_up_without_leaking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    world = _World()
    monkeypatch.setattr(
        smoke,
        "negative_checks",
        lambda *a, **k: [
            (name, next(iter(codes))) for name, codes in smoke.NEGATIVE_EXPECTED.items()
        ],
    )
    monkeypatch.setattr(smoke.time, "sleep", lambda _s: None)
    docker_calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def docker(*args: str, **kwargs: Any) -> None:
        docker_calls.append((args, kwargs))

    out: list[str] = []
    s = _smoke(world, world, out, dry_run=False, docker=docker)
    s.vpc_id, s.subnets = "vpc-0default", ["subnet-0a"]
    s.run()
    results = {step.name: step.result for step in s.report.steps}
    assert all(r == "PASS" for r in results.values()), s.report.steps
    assert "8 TEST_VAR injected (hash only)" in results
    notes = {step.name: step.note for step in s.report.steps}
    assert "code allowlist refused" in notes["9a GetSecretValue groundcrew/smoke/TEST_VAR"]
    assert "code allowlist refused" in notes["9e CreateRole groundcrew-smoke-should-not-exist"]
    assert sum(1 for n in results if n.startswith("9")) == 5
    # Cleanup in dependency order; the security group that is not ours is left alone.
    deletes = [c for c in world.log if "delete" in c]
    assert deletes == [
        "ecs.delete_service",
        "ec2.delete_security_group",
        "logs.delete_log_group",
        "ecr.delete_repository",
        "secretsmanager.delete_secret",
    ]
    assert "1 not ours, left alone" in next(
        st.note for st in s.report.steps if st.name == "10 delete security group"
    )
    # docker login reads the password from stdin, in a private DOCKER_CONFIG.
    login = next(kw for args, kw in docker_calls if args[0] == "login")
    assert login["stdin"] == "registry-password" and "DOCKER_CONFIG" in login["env"]
    assert "registry-password" not in " ".join(" ".join(a) for a, _ in docker_calls)
    # The dummy secret and the registry password appear nowhere.
    path = smoke.write_report(s.report, s.scrub, tmp_path)
    for leaked in (world.secret, "registry-password"):
        assert leaked and leaked not in "\n".join(out) and leaked not in path.read_text()
    assert s.report.ok and s.report.estimated_cost_usd < 0.01


def test_cleanup_never_deletes_what_is_not_ours() -> None:
    world = _World(ours=False)
    s = _smoke(world, world, [], dry_run=False)
    s.created = smoke.Created(
        repository=True,
        log_group=True,
        security_group="sg-0smoke",
        secret_arn=SECRET_ARN,
        service=True,
    )
    s.cleanup()
    deletes = [c for c in world.log if "delete" in c]
    # Service, security groups, and secret carry another run's tag: skipped. The repository
    # and log group were created by this run (their tags cannot be read with this role).
    assert deletes == ["logs.delete_log_group", "ecr.delete_repository"]
    skipped = {st.name for st in s.report.steps if st.result == "SKIP"}
    assert skipped == {"10 delete service", "10 delete secret"}
    assert not s.report.leftovers


def test_a_failed_delete_does_not_stop_the_rest() -> None:
    world = _World()
    s = _smoke(world, world, [], dry_run=False)
    s.created = smoke.Created(log_group=True, repository=True, secret_arn=SECRET_ARN)

    def boom(**_: Any) -> Any:
        raise ClientError(
            {"Error": {"Code": "Throttling", "Message": "slow down"}}, "DeleteLogGroup"
        )

    original = world.client

    def client(service: str, region_name: str | None = None) -> _Fake:
        fake = original(service, region_name)
        if service == "logs":
            fake.ops["delete_log_group"] = boom
        return fake

    world.client = client  # type: ignore[method-assign]
    s.cleanup()
    # The log group delete failed; the repository and secret were still deleted.
    assert [c for c in world.log if "delete" in c] == [
        "logs.delete_log_group",
        "ecr.delete_repository",
        "secretsmanager.delete_secret",
    ]
    assert s.report.leftovers and "Throttling" in s.report.leftovers[0]
    assert not s.report.ok


def test_estimated_cost() -> None:
    # 10 minutes of 0.25 vCPU / 0.5 GB + public IPv4, and a secret kept 10 minutes.
    assert smoke.estimate_cost(600, 600) == pytest.approx(0.00298, abs=0.00005)
    assert smoke.estimate_cost(0, 0) == 0
