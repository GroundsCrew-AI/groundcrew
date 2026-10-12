"""Telemetry API responses shaped like the three checkout-demo scenarios (scenarios/README.md).

AWS responses are served to real boto3 clients by tests.aws_fixture.fixture_session (the
read-only guard runs); GitHub and Vercel responses by httpx MockTransports. The incident starts
at T0 (10:00 UTC); the window Medic reads is 09:30 to 10:15.
"""

from datetime import datetime, timedelta
from typing import Any

from tests.aws_fixture import ACCOUNT, REGION

T0 = datetime.fromisoformat("2026-10-05T10:00:00+00:00")
SINCE = T0 - timedelta(minutes=30)
UNTIL = T0 + timedelta(minutes=15)
REPO = "GroundsCrew-AI/checkout-demo"
CLUSTER = f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/checkout"
TD = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition"
SECRET_ARN = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:checkout/database-url-AbCdEf"
BAD_SHA = "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432"
INDEX_SHA = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def ms(minutes: float) -> int:
    return int(at(minutes).timestamp() * 1000)


def _service(
    name: str,
    events: list[tuple[float, str]],
    deployments: list[dict[str, Any]],
    running: int,
    desired: int,
) -> dict[str, Any]:
    return {
        "serviceArn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/checkout/{name}",
        "serviceName": name,
        "clusterArn": CLUSTER,
        "status": "ACTIVE",
        "runningCount": running,
        "desiredCount": desired,
        "events": [{"createdAt": at(m), "message": msg} for m, msg in reversed(events)],
        "deployments": deployments,
    }


def _stopped(
    task_id: str,
    minutes: float,
    reason: str,
    container: str,
    code: int | None,
    container_reason: str | None,
    td: str,
) -> dict[str, Any]:
    c: dict[str, Any] = {"name": container}
    if code is not None:
        c["exitCode"] = code
    if container_reason:
        c["reason"] = container_reason
    return {
        "taskArn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/checkout/{task_id}",
        "taskDefinitionArn": f"{TD}/{td}",
        "lastStatus": "STOPPED",
        "stoppedAt": at(minutes),
        "stoppedReason": reason,
        "containers": [c],
    }


def ecs_responses(service: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ecs:DescribeServices": {"services": [service], "failures": []},
        "ecs:ListTasks": {"taskArns": [t["taskArn"] for t in tasks]},
        "ecs:DescribeTasks": {"tasks": tasks, "failures": []},
    }


def logs_response(lines: list[tuple[float, str]]) -> dict[str, Any]:
    return {
        "logs:FilterLogEvents": {
            "events": [
                {"timestamp": ms(m), "message": text, "logStreamName": "s", "eventId": str(i)}
                for i, (m, text) in enumerate(lines)
            ]
        }
    }


# --- bad-deploy-oom -------------------------------------------------------------------------

OOM = "OutOfMemoryError: Container killed due to memory usage"


def oom_aws() -> dict[str, Any]:
    service = _service(
        "checkout-worker",
        [
            (-8, "(service checkout-worker) has started 1 tasks: (task aaa111)."),
            (-5, "(service checkout-worker) has started 1 tasks: (task bbb222)."),
            (
                -2,
                "(service checkout-worker) deployment ecs-svc/2 deployment failed: "
                "tasks failed to start.",
            ),
        ],
        [
            {
                "id": "ecs-svc/2",
                "status": "PRIMARY",
                "taskDefinition": f"{TD}/checkout-worker:42",
                "desiredCount": 1,
                "runningCount": 0,
                "failedTasks": 3,
                "rolloutState": "FAILED",
                "rolloutStateReason": "ECS deployment circuit breaker: tasks failed to start.",
                "createdAt": at(-9),
                "updatedAt": at(-2),
            },
            {
                "id": "ecs-svc/1",
                "status": "ACTIVE",
                "taskDefinition": f"{TD}/checkout-worker:41",
                "desiredCount": 0,
                "runningCount": 0,
                "failedTasks": 0,
                "rolloutState": "COMPLETED",
                "createdAt": at(-600),
                "updatedAt": at(-9),
            },
        ],
        running=0,
        desired=1,
    )
    tasks = [
        _stopped(f"aaa111{i}", m, OOM, "worker", 137, OOM, "checkout-worker:42")
        for i, m in enumerate((-7, -4, -1))
    ]
    return {
        **ecs_responses(service, tasks),
        **logs_response(
            [
                (-12, "poll ok: 0 pending orders"),
                (-7.5, "worker starting"),
                (-7.1, "loading order batch"),
                (-4.5, "worker starting"),
            ]
        ),
    }


def oom_github() -> dict[str, Any]:
    return {
        f"GET /repos/{REPO}/commits": [
            {
                "sha": BAD_SHA,
                "html_url": f"https://github.com/{REPO}/commit/{BAD_SHA}",
                "commit": {
                    "message": "Tune worker memory\n\nsmaller tasks",
                    "author": {"name": "dev"},
                    "committer": {"date": "2026-10-05T09:49:00Z"},
                },
            }
        ],
        f"GET /repos/{REPO}/commits/{BAD_SHA}": {"files": [{"filename": "worker/ecs-task.json"}]},
        f"GET /repos/{REPO}/actions/runs": {
            "workflow_runs": [
                {
                    "id": 77,
                    "run_number": 41,
                    "path": ".github/workflows/deploy.yml",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "failure",
                    "head_sha": BAD_SHA,
                    "created_at": "2026-10-05T09:49:30Z",
                    "updated_at": "2026-10-05T09:59:00Z",
                    "html_url": f"https://github.com/{REPO}/actions/runs/77",
                }
            ]
        },
    }


# --- missing-index --------------------------------------------------------------------------


def index_logs() -> dict[str, Any]:
    lines: list[tuple[float, str]] = []
    for i in range(40):
        minute = -20 + i * 0.75
        slow = minute >= -5
        latency = 900 + i if slow else 3
        lines.append(
            (
                minute,
                '{"level": "info", "path": "/orders/search", "status": 200, '
                f'"latency_ms": {latency}}}',
            )
        )
        lines.append(
            (minute + 0.1, '{"level": "info", "path": "/health", "status": 200, "latency_ms": 1}')
        )
    return logs_response(lines)


def index_rds() -> dict[str, Any]:
    minutes = list(range(-30, 15))
    cpu = [5.0 if m < -5 else 88.0 for m in minutes]
    latency = [0.0008 if m < -5 else 0.025 for m in minutes]
    stamps = [at(m) for m in minutes]
    return {
        "cloudwatch:GetMetricData": {
            "MetricDataResults": [
                {"Id": "cpu", "Timestamps": stamps, "Values": cpu, "StatusCode": "Complete"},
                {
                    "Id": "connections",
                    "Timestamps": stamps,
                    "Values": [4.0] * len(stamps),
                    "StatusCode": "Complete",
                },
                {
                    "Id": "read_latency",
                    "Timestamps": stamps,
                    "Values": latency,
                    "StatusCode": "Complete",
                },
                {
                    "Id": "read_iops",
                    "Timestamps": stamps,
                    "Values": [10.0] * len(stamps),
                    "StatusCode": "Complete",
                },
            ]
        }
    }


def index_github() -> dict[str, Any]:
    return {
        f"GET /repos/{REPO}/commits": [
            {
                "sha": INDEX_SHA,
                "html_url": f"https://github.com/{REPO}/commit/{INDEX_SHA}",
                "commit": {
                    "message": "Clean up order indexes",
                    "author": {"name": "dev"},
                    "committer": {"date": "2026-10-05T09:50:00Z"},
                },
            }
        ],
        f"GET /repos/{REPO}/commits/{INDEX_SHA}": {
            "files": [{"filename": "api/migrations/versions/0002_drop_customer_email_index.py"}]
        },
        f"GET /repos/{REPO}/actions/runs": {"workflow_runs": []},
    }


# --- broken-env -----------------------------------------------------------------------------

BAD_DSN = "postgresql://checkout:hunter2@checkout-orders-db.invalid:5432/checkout"


def env_aws() -> dict[str, Any]:
    service = _service(
        "checkout-api",
        [
            (-11, "(service checkout-api) has started 1 tasks: (task new111)."),
            (-8, "(service checkout-api) has stopped 1 running tasks: (task old000)."),
            (-6, "(service checkout-api) (task new111) failed container health checks."),
            (
                -3,
                "(service checkout-api) deployment ecs-svc/9 deployment failed: "
                "tasks failed to start.",
            ),
        ],
        [
            {
                "id": "ecs-svc/9",
                "status": "PRIMARY",
                "taskDefinition": f"{TD}/checkout-api:17",
                "desiredCount": 1,
                "runningCount": 0,
                "failedTasks": 2,
                "rolloutState": "FAILED",
                "createdAt": at(-11),
                "updatedAt": at(-3),
            },
        ],
        running=0,
        desired=1,
    )
    tasks = [
        _stopped(
            "old000", -8, "Simulated Fargate task retirement", "api", 0, None, "checkout-api:17"
        ),
        _stopped(
            "new111", -6, "Task failed container health checks", "api", 1, None, "checkout-api:17"
        ),
        _stopped(
            "new222", -1, "Task failed container health checks", "api", 1, None, "checkout-api:17"
        ),
    ]
    return {
        **ecs_responses(service, tasks),
        **logs_response(
            [
                (-20, '{"level": "info", "path": "/health", "status": 200, "latency_ms": 2}'),
                (-10.5, "starting api"),
                (-10.4, f"connecting to {BAD_DSN}"),
                (-10, "health check failed: database unreachable"),
                (-9.5, "health check failed: database unreachable"),
            ]
        ),
        "secretsmanager:DescribeSecret": {
            "ARN": SECRET_ARN,
            "Name": "checkout/database-url",
            "LastChangedDate": at(-12),
            "LastAccessedDate": T0.replace(hour=0),
            "RotationEnabled": False,
            "VersionIdsToStages": {
                "bbbbbbbb-2222-bad": ["AWSCURRENT"],
                "aaaaaaaa-1111-good": ["AWSPREVIOUS"],
            },
        },
    }


def quiet_github() -> dict[str, Any]:
    return {
        f"GET /repos/{REPO}/commits": [],
        f"GET /repos/{REPO}/actions/runs": {"workflow_runs": []},
    }
