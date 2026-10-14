"""AWS discovery on the checkout demo fixture: inventory, describes, normalization, limits."""

import json
from typing import Any

import pytest

from app.connectors.aws import AwsReader
from app.discovery.aws import (
    AwsDiscovery,
    env_summary,
    github_oidc_subjects,
    inventory_type,
    keep_env_value,
    parse_image,
    repos_from_subjects,
)
from tests.aws_fixture import (
    ACCOUNT,
    API_IP,
    FIXTURE,
    GITHUB_TRUST,
    SECRET_ARN,
    SHA,
    fixture_session,
)
from tests.discovery_helpers import aws_result, settings

pytestmark = pytest.mark.anyio


def _by_name(resources: list[Any], rtype: str, name: str) -> Any:
    return next(r for r in resources if r.type == rtype and r.name == name)


async def test_discovers_the_demo_account() -> None:
    result = await aws_result()
    types = {(r.type, r.name) for r in result.resources}
    assert {
        ("ecs:cluster", "checkout"),
        ("ecs:service", "checkout-api"),
        ("ecs:service", "checkout-worker"),
        ("ecr:repository", "checkout-api"),
        ("ecr:repository", "checkout-worker"),
        ("rds:db", "checkout-orders-db"),
        ("secretsmanager:secret", "checkout/database-url"),
        ("logs:log-group", "/ecs/checkout-api"),
        ("logs:log-group", "/ecs/checkout-worker"),
        ("iam:role", "checkout-github-deploy"),
        ("iam:role", "checkout-api-task-execution"),
        ("ec2:security-group", "checkout-db"),
        ("ec2:vpc", "checkout-vpc"),
        ("s3:bucket", "groundcrew-checkout-tfstate-881050425883"),
    } <= types
    # Noise and defaults are left out; unrelated roles too.
    assert not any(t == "ec2:network-interface" for t, _ in types)
    assert ("ec2:security-group", "default") not in types
    assert ("iam:role", "AWSServiceRoleForECS") not in types
    assert ("iam:role", "groundcrew-read-only") not in types
    assert all(r.account == ACCOUNT for r in result.resources)
    assert result.notes == []


async def test_service_carries_tasks_ips_images_and_secret_refs() -> None:
    api = _by_name((await aws_result()).resources, "ecs:service", "checkout-api")
    config = api.config
    assert config["public_ips"] == [API_IP]
    assert config["has_load_balancer"] is False
    assert config["network"]["security_groups"] == ["sg-tasks", "sg-api"]
    (container,) = config["containers"]
    assert container["image_tag"] == SHA and container["image_repository"] == "checkout-api"
    assert container["ports"] == [{"port": 8000, "protocol": "tcp"}]
    assert container["secrets"] == [{"name": "DATABASE_URL", "value_from": SECRET_ARN}]
    assert container["log"]["group"] == "/ecs/checkout-api"
    assert config["running_tasks"][0]["health"] == "HEALTHY"
    # Resource Explorer tags merge into the described service.
    assert api.tags == {"app": "checkout"}


async def test_env_values_are_dropped_except_endpoints() -> None:
    api = _by_name((await aws_result()).resources, "ecs:service", "checkout-api")
    (container,) = api.config["containers"]
    assert container["env_names"] == ["LOG_LEVEL", "SENTRY_DSN", "UPSTREAM_URL"]
    # Only the endpoint survives, and without its credentials.
    assert container["env_values"] == {"UPSTREAM_URL": "http://internal.example.com:9000"}
    dumped = json.dumps(api.config)
    assert "abc123secret" not in dumped and "hunter2" not in dumped


async def test_secret_is_metadata_only() -> None:
    result = await aws_result()
    secret = _by_name(result.resources, "secretsmanager:secret", "checkout/database-url")
    assert secret.external_id == SECRET_ARN
    assert set(secret.config) == {
        "description",
        "last_changed_at",
        "last_accessed_at",
        "created_at",
        "rotation_enabled",
        "kms_key",
        "version_stages",
    }
    # Full version ids: Terraform's all start "terraform-", so 8 characters said "terrafor".
    assert secret.config["version_stages"] == {
        "11111111-aaaa": ["AWSCURRENT"],
        "22222222-bbbb": ["AWSPREVIOUS"],
    }
    assert "postgresql://" not in json.dumps([r.config for r in result.resources])


async def test_secret_values_are_never_requested() -> None:
    calls: list[str] = []
    reader = AwsReader(fixture_session(calls=calls), settings())
    await AwsDiscovery(reader, ACCOUNT, max_resources=2000).run()
    assert "secretsmanager:GetSecretValue" not in calls
    assert not any(c.startswith(("ssm:", "kms:", "s3:")) for c in calls)
    assert "resource-explorer-2:Search" in calls and "ecs:DescribeTasks" in calls


async def test_falls_back_to_tagging_api_without_resource_explorer() -> None:
    responses = dict(FIXTURE)
    responses["resource-explorer-2:GetDefaultView"] = {
        "Error": {"Code": "ResourceNotFoundException", "Message": "No index"}
    }
    responses["resourcegroupstaggingapi:GetResources"] = {
        "ResourceTagMappingList": [
            {
                "ResourceARN": "arn:aws:sqs:us-east-2:881050425883:orders-queue",
                "Tags": [{"Key": "app", "Value": "checkout"}],
            }
        ]
    }
    result = await aws_result(responses)
    assert any("tagging API" in n for n in result.notes)
    queue = _by_name(result.resources, "sqs:queue", "orders-queue")
    assert queue.tags == {"app": "checkout"} and queue.region == "us-east-2"
    assert ("ecs:service", "checkout-api") in {(r.type, r.name) for r in result.resources}


async def test_a_failing_service_does_not_stop_the_rest() -> None:
    responses = dict(FIXTURE)
    responses["rds:DescribeDBInstances"] = {"Error": {"Code": "AccessDenied", "Message": "no"}}
    result = await aws_result(responses)
    assert any(n.startswith("RDS us-east-2: skipped (AccessDenied") for n in result.notes)
    assert ("ecs:service", "checkout-api") in {(r.type, r.name) for r in result.resources}


async def test_max_resources_truncates_and_says_so() -> None:
    reader = AwsReader(fixture_session(), settings())
    result = await AwsDiscovery(reader, ACCOUNT, max_resources=3).run()
    assert result.truncated and len(result.resources) == 3
    assert any("MAX_RESOURCES_PER_CONNECTION" in n for n in result.notes)


def test_oidc_trust_parsing() -> None:
    subjects = github_oidc_subjects(GITHUB_TRUST)
    assert "repo:GroundsCrew-AI/checkout-demo:ref:refs/heads/main" in subjects
    assert repos_from_subjects(subjects) == ["GroundsCrew-AI/checkout-demo"]
    assert (
        github_oidc_subjects({"Statement": [{"Principal": {"Service": "ecs-tasks.amazonaws.com"}}]})
        == []
    )


@pytest.mark.parametrize(
    ("name", "kept"),
    [
        ("API_URL", True),
        ("REDIS_HOST", True),
        ("DATABASE_URL", False),
        ("AUTH_URL", False),
        ("API_TOKEN", False),
        ("SECRET_HOST", False),
        ("LOG_LEVEL", False),
    ],
)
def test_keep_env_value(name: str, kept: bool) -> None:
    assert keep_env_value(name) is kept


def test_env_summary_strips_userinfo() -> None:
    names, values = env_summary([{"name": "API_URL", "value": "https://u:p@api.example.com/x"}])
    assert names == ["API_URL"] and values == {"API_URL": "https://api.example.com/x"}


@pytest.mark.parametrize(
    ("arn", "rtype"),
    [
        ("arn:aws:s3:::bucket", "s3:bucket"),
        ("arn:aws:sqs:us-east-2:1:q", "sqs:queue"),
        ("arn:aws:rds:us-east-2:1:db:orders", "rds:db"),
        ("arn:aws:ecs:us-east-2:1:service/c/s", "ecs:service"),
        (
            "arn:aws:elasticloadbalancing:us-east-2:1:loadbalancer/app/x/1",
            "elasticloadbalancing:loadbalancer/app",
        ),
    ],
)
def test_inventory_type(arn: str, rtype: str) -> None:
    assert inventory_type(arn) == rtype


def test_parse_image() -> None:
    assert parse_image("123.dkr.ecr.us-east-2.amazonaws.com/checkout-api:abc") == {
        "registry": "123.dkr.ecr.us-east-2.amazonaws.com",
        "repository": "checkout-api",
        "tag": "abc",
        "digest": None,
    }
    assert parse_image("redis")["repository"] == "redis"
    assert parse_image("localhost:5000/app@sha256:1")["digest"] == "sha256:1"
