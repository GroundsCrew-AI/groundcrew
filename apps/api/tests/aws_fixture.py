"""AWS responses for the checkout demo account, served to real boto3 clients.

`fixture_session()` returns a boto3 session with the production read-only guard installed
and a botocore `before-call` handler that answers from FIXTURE instead of the network, so the
real client code (parameter validation, paginators, the allowlist hook) runs in tests. Calls
are unordered (discovery runs them in parallel threads), which botocore's Stubber cannot do;
tests/test_aws_connector.py uses the Stubber for the ordered STS flows.

Shapes follow the AWS API responses for ECS, ECR, RDS, EC2, IAM, Logs, Secrets Manager,
Resource Explorer, and Cloud Control. Values mirror GroundsCrew-AI/checkout-demo.
"""

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import boto3

from app.connectors.aws import AWS_READ_OPERATIONS, guard_session

ACCOUNT = "881050425883"
REGION = "us-east-2"
SHA = "cef79e7a1b2c3d4e5f60718293a4b5c6d7e8f901"
API_IP = "3.15.10.20"
WORKER_IP = "18.217.5.6"
REGISTRY = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"
CLUSTER = f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/checkout"
API_SERVICE = f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/checkout/checkout-api"
WORKER_SERVICE = f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/checkout/checkout-worker"
API_TD = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/checkout-api:7"
WORKER_TD = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/checkout-worker:7"
SECRET_ARN = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:checkout/database-url-AbCdEf"
DB_ARN = f"arn:aws:rds:{REGION}:{ACCOUNT}:db:checkout-orders-db"
DEPLOY_ROLE = f"arn:aws:iam::{ACCOUNT}:role/checkout-github-deploy"
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)

Responder = Callable[[dict[str, Any]], Any]


def _container(svc: str, port: int | None) -> dict[str, Any]:
    container: dict[str, Any] = {
        "name": svc,
        "image": f"{REGISTRY}/checkout-{svc}:{SHA}",
        "essential": True,
        "environment": [
            {"name": "LOG_LEVEL", "value": "INFO"},
            # Values that must never leave discovery: a DSN and a token-ish name.
            {"name": "SENTRY_DSN", "value": "https://abc123secret@o1.ingest.sentry.io/1"},
            {"name": "UPSTREAM_URL", "value": "http://svc:hunter2@internal.example.com:9000"},
        ],
        "secrets": [{"name": "DATABASE_URL", "valueFrom": SECRET_ARN}],
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": f"/ecs/checkout-{svc}",
                "awslogs-region": REGION,
                "awslogs-stream-prefix": svc,
            },
        },
    }
    if port:
        container["portMappings"] = [{"containerPort": port, "protocol": "tcp"}]
        container["healthCheck"] = {"command": ["CMD", "true"]}
    return container


def _task_definition(svc: str, port: int | None) -> dict[str, Any]:
    return {
        "taskDefinition": {
            "taskDefinitionArn": API_TD if svc == "api" else WORKER_TD,
            "family": f"checkout-{svc}",
            "revision": 7,
            "cpu": "256",
            "memory": "512",
            "executionRoleArn": f"arn:aws:iam::{ACCOUNT}:role/checkout-{svc}-task-execution",
            "containerDefinitions": [_container(svc, port)],
        }
    }


def _service(svc: str, sgs: list[str]) -> dict[str, Any]:
    return {
        "serviceArn": API_SERVICE if svc == "api" else WORKER_SERVICE,
        "serviceName": f"checkout-{svc}",
        "clusterArn": CLUSTER,
        "status": "ACTIVE",
        "launchType": "FARGATE",
        "desiredCount": 1,
        "runningCount": 1,
        "pendingCount": 0,
        "taskDefinition": API_TD if svc == "api" else WORKER_TD,
        "loadBalancers": [],
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": ["subnet-a", "subnet-b"],
                "securityGroups": sgs,
                "assignPublicIp": "ENABLED",
            }
        },
        "deployments": [
            {
                "status": "PRIMARY",
                "rolloutState": "COMPLETED",
                "taskDefinition": API_TD if svc == "api" else WORKER_TD,
                "desiredCount": 1,
                "runningCount": 1,
                "failedTasks": 0,
                "createdAt": NOW,
                "updatedAt": NOW,
            }
        ],
        "events": [
            {"createdAt": NOW, "message": f"(service checkout-{svc}) has reached a steady state."}
        ],
        "deploymentConfiguration": {
            "deploymentCircuitBreaker": {"enable": True, "rollback": False}
        },
    }


def _task(svc: str, eni: str) -> dict[str, Any]:
    return {
        "taskArn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/checkout/{svc}0001",
        "taskDefinitionArn": API_TD if svc == "api" else WORKER_TD,
        "group": f"service:checkout-{svc}",
        "lastStatus": "RUNNING",
        "healthStatus": "HEALTHY" if svc == "api" else "UNKNOWN",
        "startedAt": NOW,
        "attachments": [
            {
                "type": "ElasticNetworkInterface",
                "details": [{"name": "networkInterfaceId", "value": eni}],
            }
        ],
        "containers": [{"name": svc, "image": f"{REGISTRY}/checkout-{svc}:{SHA}"}],
    }


def _encoded(doc: dict[str, Any]) -> str:
    return quote(json.dumps(doc))


def _role(name: str, trust: dict[str, Any]) -> dict[str, Any]:
    return {
        "RoleName": name,
        "Arn": f"arn:aws:iam::{ACCOUNT}:role/{name}",
        "CreateDate": NOW,
        "MaxSessionDuration": 3600,
        # IAM returns policy documents URL-encoded; botocore decodes them after the call.
        "AssumeRolePolicyDocument": _encoded(trust),
    }


ECS_TRUST = {
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "ecs-tasks.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ]
}
OIDC = f"arn:aws:iam::{ACCOUNT}:oidc-provider/token.actions.githubusercontent.com"
GITHUB_TRUST = {
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Federated": OIDC},
            "Action": "sts:AssumeRoleWithWebIdentity",
            "Condition": {
                "StringEquals": {
                    "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                    "token.actions.githubusercontent.com:sub": [
                        "repo:GroundsCrew-AI/checkout-demo:ref:refs/heads/main",
                        "repo:GroundsCrew-AI@338275343/checkout-demo@1408958338:ref:refs/heads/main",
                    ],
                }
            },
        }
    ]
}
DEPLOY_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "EcrAuth",
            "Effect": "Allow",
            "Action": "ecr:GetAuthorizationToken",
            "Resource": "*",
        },
        {
            "Sid": "DeployServices",
            "Effect": "Allow",
            "Action": ["ecs:DescribeServices", "ecs:UpdateService"],
            "Resource": [WORKER_SERVICE, API_SERVICE],
        },
    ],
}


def _by(param: str, table: dict[str, Any]) -> Responder:
    return lambda params: table.get(str(params.get(param)))


FIXTURE: dict[str, Any] = {
    "resource-explorer-2:GetDefaultView": {
        "ViewArn": f"arn:aws:resource-explorer-2:{REGION}:{ACCOUNT}:view/groundcrew-default/1"
    },
    "resource-explorer-2:Search": {
        "Resources": [
            {
                "Arn": "arn:aws:s3:::groundcrew-checkout-tfstate-881050425883",
                "ResourceType": "s3:bucket",
                "Region": REGION,
                "OwningAccountId": ACCOUNT,
                "Service": "s3",
                "Properties": [
                    {"Name": "tags", "Data": [{"Key": "project", "Value": "groundcrew"}]}
                ],
            },
            {
                "Arn": f"arn:aws:ec2:{REGION}:{ACCOUNT}:network-interface/eni-api",
                "ResourceType": "ec2:network-interface",
                "Region": REGION,
                "OwningAccountId": ACCOUNT,
                "Service": "ec2",
                "Properties": [],
            },
            {
                "Arn": API_SERVICE,
                "ResourceType": "ecs:service",
                "Region": REGION,
                "OwningAccountId": ACCOUNT,
                "Service": "ecs",
                "Properties": [{"Name": "tags", "Data": [{"Key": "app", "Value": "checkout"}]}],
            },
        ]
    },
    "cloudcontrol:GetResource": {
        "TypeName": "AWS::S3::Bucket",
        "ResourceDescription": {
            "Identifier": "groundcrew-checkout-tfstate-881050425883",
            "Properties": json.dumps(
                {
                    "BucketName": "groundcrew-checkout-tfstate-881050425883",
                    "VersioningConfiguration": {"Status": "Enabled"},
                }
            ),
        },
    },
    "ecs:ListClusters": {"clusterArns": [CLUSTER]},
    "ecs:DescribeClusters": {
        "clusters": [
            {
                "clusterArn": CLUSTER,
                "clusterName": "checkout",
                "status": "ACTIVE",
                "runningTasksCount": 2,
                "activeServicesCount": 2,
                "settings": [{"name": "containerInsights", "value": "disabled"}],
            }
        ]
    },
    "ecs:ListServices": {"serviceArns": [API_SERVICE, WORKER_SERVICE]},
    "ecs:DescribeServices": {
        "services": [_service("api", ["sg-tasks", "sg-api"]), _service("worker", ["sg-tasks"])]
    },
    "ecs:ListTasks": {"taskArns": ["t-api", "t-worker"]},
    "ecs:DescribeTasks": {"tasks": [_task("api", "eni-api"), _task("worker", "eni-worker")]},
    "ecs:DescribeTaskDefinition": _by(
        "taskDefinition",
        {API_TD: _task_definition("api", 8000), WORKER_TD: _task_definition("worker", None)},
    ),
    "ec2:DescribeNetworkInterfaces": {
        "NetworkInterfaces": [
            {
                "NetworkInterfaceId": "eni-api",
                "PrivateIpAddress": "10.20.0.15",
                "Association": {"PublicIp": API_IP},
            },
            {
                "NetworkInterfaceId": "eni-worker",
                "PrivateIpAddress": "10.20.1.22",
                "Association": {"PublicIp": WORKER_IP},
            },
        ]
    },
    "ecr:DescribeRepositories": {
        "repositories": [
            {
                "repositoryArn": f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/checkout-{svc}",
                "repositoryName": f"checkout-{svc}",
                "repositoryUri": f"{REGISTRY}/checkout-{svc}",
                "imageTagMutability": "MUTABLE",
                "imageScanningConfiguration": {"scanOnPush": True},
            }
            for svc in ("api", "worker")
        ]
    },
    "ecr:DescribeImages": {
        "imageDetails": [
            {
                "imageDigest": "sha256:aaa",
                "imageTags": [SHA],
                "imagePushedAt": NOW,
                "imageSizeInBytes": 60_000_000,
            }
        ]
    },
    "rds:DescribeDBInstances": {
        "DBInstances": [
            {
                "DBInstanceIdentifier": "checkout-orders-db",
                "DBInstanceArn": DB_ARN,
                "DBInstanceClass": "db.t4g.micro",
                "Engine": "postgres",
                "EngineVersion": "16.4",
                "DBInstanceStatus": "available",
                "DBName": "checkout",
                "Endpoint": {
                    "Address": "checkout-orders-db.c1abc2def3gh.us-east-2.rds.amazonaws.com",
                    "Port": 5432,
                },
                "PubliclyAccessible": False,
                "MultiAZ": False,
                "StorageEncrypted": True,
                "DeletionProtection": False,
                "DBSubnetGroup": {"VpcId": "vpc-0a1"},
                "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-db", "Status": "active"}],
                "TagList": [{"Key": "Name", "Value": "checkout-orders-db"}],
            }
        ]
    },
    "ec2:DescribeVpcs": {
        "Vpcs": [
            {
                "VpcId": "vpc-0a1",
                "CidrBlock": "10.20.0.0/16",
                "IsDefault": False,
                "Tags": [{"Key": "Name", "Value": "checkout-vpc"}],
            },
            {"VpcId": "vpc-default", "CidrBlock": "172.31.0.0/16", "IsDefault": True},
        ]
    },
    "ec2:DescribeSubnets": {
        "Subnets": [
            {
                "SubnetId": "subnet-a",
                "VpcId": "vpc-0a1",
                "AvailabilityZone": "us-east-2a",
                "CidrBlock": "10.20.0.0/24",
            },
            {
                "SubnetId": "subnet-b",
                "VpcId": "vpc-0a1",
                "AvailabilityZone": "us-east-2b",
                "CidrBlock": "10.20.1.0/24",
            },
        ]
    },
    "ec2:DescribeSecurityGroups": {
        "SecurityGroups": [
            {
                "GroupId": "sg-tasks",
                "GroupName": "checkout-tasks",
                "VpcId": "vpc-0a1",
                "IpPermissions": [],
                "IpPermissionsEgress": [
                    {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
                ],
            },
            {
                "GroupId": "sg-api",
                "GroupName": "checkout-api",
                "VpcId": "vpc-0a1",
                "IpPermissions": [
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 8000,
                        "ToPort": 8000,
                        "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "api HTTP, demo only"}],
                    }
                ],
            },
            {
                "GroupId": "sg-db",
                "GroupName": "checkout-db",
                "VpcId": "vpc-0a1",
                "IpPermissions": [
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 5432,
                        "ToPort": 5432,
                        "UserIdGroupPairs": [
                            {"GroupId": "sg-tasks", "Description": "Postgres from ECS tasks"}
                        ],
                    }
                ],
            },
            {
                "GroupId": "sg-default",
                "GroupName": "default",
                "VpcId": "vpc-0a1",
                "IpPermissions": [],
            },
        ]
    },
    "lambda:ListFunctions": {"Functions": []},
    "logs:DescribeLogGroups": {
        "logGroups": [
            {
                "logGroupName": "/ecs/checkout-api",
                "arn": f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/ecs/checkout-api:*",
                "retentionInDays": 7,
            },
            {
                "logGroupName": "/ecs/checkout-worker",
                "arn": f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/ecs/checkout-worker:*",
                "retentionInDays": 7,
            },
        ]
    },
    "secretsmanager:ListSecrets": {
        "SecretList": [
            {
                "ARN": SECRET_ARN,
                "Name": "checkout/database-url",
                "Description": "DATABASE_URL for the checkout api and worker",
                "LastChangedDate": NOW,
                "CreatedDate": NOW,
                "SecretVersionsToStages": {
                    "11111111-aaaa": ["AWSCURRENT"],
                    "22222222-bbbb": ["AWSPREVIOUS"],
                },
            }
        ]
    },
    "iam:ListRoles": {
        "Roles": [
            _role("checkout-api-task-execution", ECS_TRUST),
            _role("checkout-worker-task-execution", ECS_TRUST),
            _role("checkout-github-deploy", GITHUB_TRUST),
            _role(
                "groundcrew-read-only",
                {
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"AWS": "arn:aws:iam::881050425883:user/groundcrew-demo"},
                        }
                    ]
                },
            ),
            _role("AWSServiceRoleForECS", ECS_TRUST),
        ]
    },
    "iam:ListRolePolicies": _by(
        "RoleName",
        {
            "checkout-github-deploy": {"PolicyNames": ["deploy"]},
            "checkout-api-task-execution": {"PolicyNames": ["task-execution"]},
            "checkout-worker-task-execution": {"PolicyNames": []},
        },
    ),
    "iam:GetRolePolicy": _by(
        "RoleName",
        {
            "checkout-github-deploy": {
                "RoleName": "checkout-github-deploy",
                "PolicyName": "deploy",
                "PolicyDocument": _encoded(DEPLOY_POLICY),
            },
            "checkout-api-task-execution": {
                "RoleName": "checkout-api-task-execution",
                "PolicyName": "task-execution",
                "PolicyDocument": _encoded(
                    {
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": "secretsmanager:GetSecretValue",
                                "Resource": SECRET_ARN,
                            }
                        ]
                    }
                ),
            },
        },
    ),
    "iam:ListAttachedRolePolicies": {"AttachedPolicies": []},
}

# The demo has no Aurora cluster and no load balancer (its services run without one).
FIXTURE["rds:DescribeDBClusters"] = {"DBClusters": []}
FIXTURE["elbv2:DescribeLoadBalancers"] = {"LoadBalancers": []}

# GetRole: the listed role (discovery reads its tags there; ListRoles leaves them out).
FIXTURE["iam:GetRole"] = lambda p: {
    "Role": next(r for r in FIXTURE["iam:ListRoles"]["Roles"] if r["RoleName"] == p["RoleName"])
}


class _Http:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.raw = None
        self.content = b""


def fixture_session(
    responses: dict[str, Any] | None = None,
    calls: list[str] | None = None,
    allowed: frozenset[str] = AWS_READ_OPERATIONS,
) -> boto3.Session:
    """A session guarded by `allowed` (default: the read-only allowlist), answering from
    `responses` (default FIXTURE). Records every operation it is asked for in `calls`."""
    table = FIXTURE if responses is None else responses
    seen = calls if calls is not None else []
    session = guard_session(
        boto3.Session(
            aws_access_key_id="AKIAFAKEFAKEFAKEFAKE",
            aws_secret_access_key="fake",  # noqa: S106
            aws_session_token="fake",  # noqa: S106
            region_name=REGION,
        ),
        allowed,
    )

    def keep_params(params: dict[str, Any], context: dict[str, Any], **_: Any) -> None:
        context["fixture_params"] = dict(params)

    def respond(model: Any, context: dict[str, Any], **_: Any) -> tuple[_Http, dict[str, Any]]:
        name = f"{model.service_model.service_name}:{model.name}"
        seen.append(name)
        value = table.get(name)
        if callable(value):
            value = value(context.get("fixture_params") or {})
        if value is None:
            return _Http(400), {
                "Error": {"Code": "ResourceNotFoundException", "Message": f"no fixture for {name}"}
            }
        if isinstance(value, dict) and "Error" in value:
            return _Http(400), copy.deepcopy(value)
        return _Http(200), copy.deepcopy(value)

    session.events.register("before-parameter-build", keep_params)
    session.events.register("before-call", respond)
    return session
