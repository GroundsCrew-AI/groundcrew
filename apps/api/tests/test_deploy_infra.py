"""The Phase 4 deploy infrastructure contract: settings, names, tags, and subnets."""

import uuid

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.connectors.deploy_infra import (
    DeployInfraError,
    aws_tags,
    ecr_repository,
    ecs_service,
    k8s_labels,
    log_group,
    namespace,
    required_tags,
    resolve_subnets,
    task_execution_role_arn,
)
from tests.aws_fixture import fixture_session

RUN = uuid.UUID("0b9d4c1e-2f3a-4b5c-8d7e-6f5a4b3c2d1e")
DEFAULT_VPC = {"Vpcs": [{"VpcId": "vpc-0default", "IsDefault": True}]}
DEFAULT_SUBNETS = {
    "Subnets": [
        {"SubnetId": "subnet-0b", "VpcId": "vpc-0default", "DefaultForAz": True},
        {"SubnetId": "subnet-0a", "VpcId": "vpc-0default", "DefaultForAz": True},
    ]
}


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_defaults_match_the_connect_template_and_kind() -> None:
    s = _settings()
    assert s.gc_aws_region == "us-east-2"
    assert s.gc_ecs_cluster == "groundcrew-deploys"
    assert s.gc_task_execution_role_arn is None
    assert s.gc_subnet_ids == []
    assert s.gc_log_group_prefix == "/groundcrew/"
    assert s.gc_ecr_prefix == "groundcrew-"
    assert s.gc_kind_registry == "localhost:5001"
    assert s.gc_namespace_prefix == "gc-"


def test_settings_parse_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GC_SUBNET_IDS", "subnet-1, subnet-2")
    monkeypatch.setenv("GC_ECS_CLUSTER", "groundcrew-staging")
    monkeypatch.setenv("GC_KIND_REGISTRY", "localhost:5050")
    s = Settings(_env_file=None)
    assert s.gc_subnet_ids == ["subnet-1", "subnet-2"]
    assert s.gc_ecs_cluster == "groundcrew-staging" and s.gc_kind_registry == "localhost:5050"


@pytest.mark.parametrize(
    "field,value",
    [
        ("gc_ecs_cluster", "checkout"),
        ("gc_ecr_prefix", "checkout-"),
        ("gc_log_group_prefix", "/ecs/"),
        ("gc_namespace_prefix", "default"),
        (
            "gc_task_execution_role_arn",
            "arn:aws:iam::881050425883:role/checkout-api-task-execution",
        ),
    ],
)
def test_values_the_policies_would_deny_are_refused(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: value})


def test_tags_and_labels() -> None:
    assert required_tags(RUN) == {"project": "groundcrew", "groundcrew:deploy": str(RUN)}
    assert {"Key": "project", "Value": "groundcrew"} in aws_tags(RUN)
    assert k8s_labels(RUN) == {"project": "groundcrew", "groundcrew/deploy": str(RUN)}


def test_names_are_prefixed_and_safe() -> None:
    s = _settings()
    assert ecr_repository(s, "Shop API") == "groundcrew-shop-api"
    assert log_group(s, "shop_api") == "/groundcrew/shop-api"
    assert namespace(s, "shop.api") == "gc-shop-api"
    assert ecs_service("--Shop--API--") == "shop-api"
    long = namespace(s, "x" * 100)
    assert len(long) == 63 and long.startswith("gc-")
    with pytest.raises(DeployInfraError):
        namespace(s, "!!!")


def test_task_execution_role_arn() -> None:
    assert (
        task_execution_role_arn(_settings(), "881050425883")
        == "arn:aws:iam::881050425883:role/groundcrew-task-execution"
    )
    pinned = "arn:aws:iam::111122223333:role/groundcrew-task-execution"
    assert task_execution_role_arn(_settings(gc_task_execution_role_arn=pinned), "x") == pinned
    with pytest.raises(DeployInfraError):
        task_execution_role_arn(_settings(), "not-an-account")


def test_default_vpc_subnets() -> None:
    calls: list[str] = []
    session = fixture_session(
        {"ec2:DescribeVpcs": DEFAULT_VPC, "ec2:DescribeSubnets": DEFAULT_SUBNETS}, calls
    )
    assert resolve_subnets(session, _settings()) == ["subnet-0a", "subnet-0b"]
    assert calls == ["ec2:DescribeVpcs", "ec2:DescribeSubnets"]


def test_no_default_vpc_fails_with_the_fix() -> None:
    session = fixture_session({"ec2:DescribeVpcs": {"Vpcs": []}})
    with pytest.raises(DeployInfraError, match="no default VPC in us-east-2") as err:
        resolve_subnets(session, _settings())
    assert "GC_SUBNET_IDS" in str(err.value) and "create-default-vpc" in str(err.value)


def test_default_vpc_without_default_subnets_fails() -> None:
    session = fixture_session(
        {"ec2:DescribeVpcs": DEFAULT_VPC, "ec2:DescribeSubnets": {"Subnets": []}}
    )
    with pytest.raises(DeployInfraError, match="no default subnets"):
        resolve_subnets(session, _settings())


def test_subnet_override_is_checked_and_skips_the_default_vpc() -> None:
    calls: list[str] = []
    found = {"Subnets": [{"SubnetId": "subnet-1"}, {"SubnetId": "subnet-2"}]}
    session = fixture_session({"ec2:DescribeSubnets": found}, calls)
    s = _settings(gc_subnet_ids="subnet-1,subnet-2")
    assert resolve_subnets(session, s) == ["subnet-1", "subnet-2"]
    assert calls == ["ec2:DescribeSubnets"]
    missing = fixture_session({"ec2:DescribeSubnets": {"Subnets": [{"SubnetId": "subnet-1"}]}})
    with pytest.raises(DeployInfraError, match="subnet-2"):
        resolve_subnets(missing, s)


def test_aws_errors_become_deploy_infra_errors() -> None:
    session = fixture_session({})  # every call fails
    with pytest.raises(DeployInfraError, match="Could not read subnets"):
        resolve_subnets(session, _settings())
