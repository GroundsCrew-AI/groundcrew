"""infra/aws-connect.yaml, rendered offline for each Tier.

- Tier=observe renders exactly what the template rendered before tiers existed (snapshot of
  main's template, for both CreateResourceExplorerIndex values).
- Tier=deploy only adds the cluster and two roles, and every action the deploy role is allowed
  is listed below with its scope: a new action, or a wider scope, fails this test.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from tests.cfn_render import load, render

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = load(ROOT / "infra" / "aws-connect.yaml")
SNAPSHOT = json.loads((Path(__file__).parent / "fixtures" / "aws-connect-observe.json").read_text())

CLUSTER = {"Fn::GetAtt": ["DeployCluster", "Arn"]}
EXECUTION_ROLE = {"Fn::GetAtt": ["TaskExecutionRole", "Arn"]}


def _sub(arn: str) -> dict[str, str]:
    return {"Fn::Sub": "arn:${AWS::Partition}:" + arn}


SERVICES = _sub("ecs:${AWS::Region}:${AWS::AccountId}:service/groundcrew-deploys/*")
TASKS = _sub("ecs:${AWS::Region}:${AWS::AccountId}:task/groundcrew-deploys/*")
TASK_DEFS = _sub("ecs:${AWS::Region}:${AWS::AccountId}:task-definition/groundcrew-*:*")
REPOS = _sub("ecr:${AWS::Region}:${AWS::AccountId}:repository/groundcrew-*")
SGS = _sub("ec2:${AWS::Region}:${AWS::AccountId}:security-group/*")
VPCS = _sub("ec2:${AWS::Region}:${AWS::AccountId}:vpc/*")
LOGS = _sub("logs:${AWS::Region}:${AWS::AccountId}:log-group:/groundcrew/*")
SECRETS = _sub("secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:groundcrew/*")
IN_CLUSTER = {"ArnEquals": {"ecs:cluster": CLUSTER}}
TAGGED_REQUEST = {"StringEquals": {"aws:RequestTag/project": "groundcrew"}}
TAGGED_RESOURCE = {"StringEquals": {"aws:ResourceTag/project": "groundcrew"}}

# Every Allow in groundcrew-deploy: action -> (resource, condition). "*" with no condition is
# only for read actions AWS gives no resource-level permissions or condition keys for; no write
# action is unscoped (ecs:DeregisterTaskDefinition is not granted for that reason).
DEPLOY_ACTIONS: dict[str, tuple[Any, Any]] = {
    "ecs:CreateService": (SERVICES, None),
    "ecs:UpdateService": (SERVICES, None),
    "ecs:DeleteService": (SERVICES, None),
    "ecs:DescribeServices": (SERVICES, None),
    "ecs:DescribeClusters": (CLUSTER, None),
    "ecs:ListServices": ("*", IN_CLUSTER),
    "ecs:ListTasks": ("*", IN_CLUSTER),
    "ecs:RunTask": (TASK_DEFS, IN_CLUSTER),
    "ecs:StopTask": (TASKS, None),
    "ecs:DescribeTasks": (TASKS, None),
    "ecs:RegisterTaskDefinition": ("*", TAGGED_REQUEST),
    "ecs:DescribeTaskDefinition": ("*", None),
    "ecs:ListTaskDefinitions": ("*", None),
    "ecs:TagResource": (
        "*",
        {
            "StringEquals": {
                "ecs:CreateAction": ["CreateService", "RegisterTaskDefinition", "RunTask"],
                "aws:RequestTag/project": "groundcrew",
            }
        },
    ),
    "ecr:GetAuthorizationToken": ("*", None),
    "ecr:CreateRepository": (REPOS, TAGGED_REQUEST),
    "ecr:TagResource": (REPOS, TAGGED_REQUEST),
    **{
        f"ecr:{a}": (REPOS, None)
        for a in (
            "BatchCheckLayerAvailability",
            "InitiateLayerUpload",
            "UploadLayerPart",
            "CompleteLayerUpload",
            "PutImage",
            "BatchGetImage",
            "GetDownloadUrlForLayer",
            "DescribeRepositories",
            "DescribeImages",
            "ListImages",
            "BatchDeleteImage",
            "DeleteRepository",
        )
    },
    **{
        f"ec2:{a}": ("*", None)
        for a in (
            "DescribeVpcs",
            "DescribeSubnets",
            "DescribeSecurityGroups",
            "DescribeSecurityGroupRules",
            "DescribeNetworkInterfaces",
        )
    },
    # Two statements: the new group must be tagged; the VPC it goes in is any in the region.
    "ec2:CreateSecurityGroup": ([SGS, VPCS], [TAGGED_REQUEST, None]),
    "ec2:CreateTags": (SGS, {"StringEquals": {"ec2:CreateAction": "CreateSecurityGroup"}}),
    "ec2:AuthorizeSecurityGroupIngress": (SGS, TAGGED_RESOURCE),
    "ec2:RevokeSecurityGroupIngress": (SGS, TAGGED_RESOURCE),
    "ec2:DeleteSecurityGroup": (SGS, TAGGED_RESOURCE),
    **{
        f"logs:{a}": (LOGS, None)
        for a in (
            "CreateLogGroup",
            "DeleteLogGroup",
            "PutRetentionPolicy",
            "TagResource",
            "TagLogGroup",
            "FilterLogEvents",
        )
    },
    "logs:DescribeLogGroups": ("*", None),
    "iam:PassRole": (
        EXECUTION_ROLE,
        {"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}},
    ),
    "secretsmanager:CreateSecret": (SECRETS, TAGGED_REQUEST),
    "secretsmanager:PutSecretValue": (SECRETS, None),
    "secretsmanager:TagResource": (SECRETS, None),
    "secretsmanager:DeleteSecret": (SECRETS, None),
    "secretsmanager:DescribeSecret": (SECRETS, None),
    "tag:GetResources": ("*", None),
}

# Actions that only read: the only ones allowed to be unscoped ("*" with no condition).
READ_PREFIXES = ("Describe", "List", "Get")

DENY = ["secretsmanager:GetSecretValue", "ssm:GetParameter*", "kms:Decrypt", "s3:GetObject",
        "s3:GetObjectVersion"]  # fmt: skip


def _deploy() -> dict[str, Any]:
    return render(TEMPLATE, {"Tier": "deploy"})


def _statements(role: dict[str, Any]) -> list[dict[str, Any]]:
    (policy,) = role["Properties"]["Policies"]
    statements: list[dict[str, Any]] = policy["PolicyDocument"]["Statement"]
    return statements


def _actions(statement: dict[str, Any]) -> list[str]:
    action = statement["Action"]
    return [action] if isinstance(action, str) else list(action)


@pytest.mark.parametrize("index", ["true", "false"])
def test_observe_renders_exactly_the_template_before_tiers(index: str) -> None:
    rendered = render(TEMPLATE, {"Tier": "observe", "CreateResourceExplorerIndex": index})
    assert rendered == SNAPSHOT[f"CreateResourceExplorerIndex={index}"]


def test_observe_is_the_default() -> None:
    assert TEMPLATE["Parameters"]["Tier"]["Default"] == "observe"
    assert TEMPLATE["Parameters"]["Tier"]["AllowedValues"] == ["observe", "deploy"]
    assert render(TEMPLATE) == SNAPSHOT["CreateResourceExplorerIndex=true"]


def test_deploy_only_adds_the_cluster_and_two_roles() -> None:
    observe, deploy = render(TEMPLATE, {"Tier": "observe"}), _deploy()
    for section in ("Resources", "Outputs"):
        for name, body in observe[section].items():
            assert deploy[section][name] == body, name
    assert set(deploy["Resources"]) - set(observe["Resources"]) == {
        "DeployCluster",
        "TaskExecutionRole",
        "DeployRole",
    }
    assert set(deploy["Outputs"]) - set(observe["Outputs"]) == {
        "DeployRoleArn",
        "ClusterName",
        "TaskExecutionRoleArn",
    }
    resources = deploy["Resources"]
    assert resources["DeployCluster"]["Properties"]["ClusterName"] == "groundcrew-deploys"
    assert resources["DeployRole"]["Properties"]["RoleName"] == "groundcrew-deploy"
    assert resources["TaskExecutionRole"]["Properties"]["RoleName"] == "groundcrew-task-execution"
    for name in ("DeployCluster", "TaskExecutionRole", "DeployRole"):
        assert {"Key": "project", "Value": "groundcrew"} in resources[name]["Properties"]["Tags"]


def test_deploy_role_has_the_same_trust_as_the_read_only_role() -> None:
    resources = _deploy()["Resources"]
    read, deploy = resources["GroundcrewRole"]["Properties"], resources["DeployRole"]["Properties"]
    assert deploy["AssumeRolePolicyDocument"] == read["AssumeRolePolicyDocument"]
    assert deploy["MaxSessionDuration"] == 3600


def test_every_deploy_action_has_its_expected_scope() -> None:
    found: dict[str, list[tuple[Any, Any]]] = {}
    for st in _statements(_deploy()["Resources"]["DeployRole"]):
        if st["Effect"] != "Allow":
            continue
        for action in _actions(st):
            found.setdefault(action, []).append((st["Resource"], st.get("Condition")))
    assert set(found) == set(DEPLOY_ACTIONS), set(found) ^ set(DEPLOY_ACTIONS)
    for action, scopes in found.items():
        resource, condition = DEPLOY_ACTIONS[action]
        if isinstance(resource, list):
            assert scopes == list(zip(resource, condition, strict=True)), action
        else:
            assert scopes == [(resource, condition)], action
    assert not any("*" in a.split(":", 1)[1] for a in found)  # no wildcard actions


def test_the_deploy_role_has_no_unscoped_write_action() -> None:
    for action, (resource, condition) in DEPLOY_ACTIONS.items():
        unscoped = resource == "*" and condition is None
        verb = action.split(":", 1)[1]
        if unscoped:
            assert verb.startswith(READ_PREFIXES), f"{action} is a write with no scope"
    assert "ecs:DeregisterTaskDefinition" not in DEPLOY_ACTIONS
    assert "secretsmanager:GetSecretValue" not in DEPLOY_ACTIONS


def test_deploy_role_keeps_the_explicit_deny_on_secret_values() -> None:
    resources = _deploy()["Resources"]
    deny = [s for s in _statements(resources["DeployRole"]) if s["Effect"] == "Deny"]
    read_deny = [s for s in _statements(resources["GroundcrewRole"]) if s["Effect"] == "Deny"]
    assert deny == read_deny
    assert deny[0]["Action"] == DENY and deny[0]["Resource"] == "*"


def test_task_execution_role_only_pulls_images_writes_logs_and_reads_groundcrew_secrets() -> None:
    role = _deploy()["Resources"]["TaskExecutionRole"]["Properties"]
    (trust,) = role["AssumeRolePolicyDocument"]["Statement"]
    assert trust["Principal"] == {"Service": "ecs-tasks.amazonaws.com"}
    assert trust["Condition"]["StringEquals"] == {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}
    scopes = {a: s["Resource"] for s in _statements({"Properties": role}) for a in _actions(s)}
    assert scopes == {
        "ecr:GetAuthorizationToken": "*",
        "ecr:BatchCheckLayerAvailability": REPOS,
        "ecr:BatchGetImage": REPOS,
        "ecr:GetDownloadUrlForLayer": REPOS,
        "logs:CreateLogStream": LOGS,
        "logs:PutLogEvents": LOGS,
        "secretsmanager:GetSecretValue": SECRETS,
    }


def test_code_allowlist_matches_the_deploy_policy() -> None:
    """Every operation a deploy session may call is allowed by the deploy role, and the role
    allows nothing an API client calls that the allowlist would refuse (except image layer
    pushes and pulls, which go through the registry, and logs:TagLogGroup, the legacy alias)."""
    from app.connectors.aws import AWS_DEPLOY_OPERATIONS, FORBIDDEN_OPERATIONS

    iam_name = {"resourcegroupstaggingapi": "tag", "sts": "sts"}
    code = {
        f"{iam_name.get(op.split(':')[0], op.split(':')[0])}:{op.split(':')[1]}"
        for op in AWS_DEPLOY_OPERATIONS
    } - {"sts:GetCallerIdentity"}  # needs no permission
    registry_only = {
        f"ecr:{a}"
        for a in (
            "BatchCheckLayerAvailability",
            "InitiateLayerUpload",
            "UploadLayerPart",
            "CompleteLayerUpload",
            "PutImage",
            "BatchGetImage",
            "GetDownloadUrlForLayer",
        )
    }
    assert code == set(DEPLOY_ACTIONS) - registry_only - {"logs:TagLogGroup", "iam:PassRole"}
    assert not AWS_DEPLOY_OPERATIONS & FORBIDDEN_OPERATIONS
    assert not any(op.startswith(("iam:", "rds:", "s3:", "kms:", "ssm:"))
                   for op in AWS_DEPLOY_OPERATIONS)  # fmt: skip
    assert "secretsmanager:GetSecretValue" not in AWS_DEPLOY_OPERATIONS
    assert "ecs:DeregisterTaskDefinition" not in AWS_DEPLOY_OPERATIONS
