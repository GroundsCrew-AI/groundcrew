"""Fakes for the deploy target tests: a Kubernetes API, an AWS session, and a sandbox.

All three append to one shared `events` list, so a test can check the order of what happened
across them (the image push must come before any infrastructure write)."""

import base64
import copy
import uuid
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from app.db.models import Approval, ApprovalStatus, Run, RunStatus, ToolPolicy, Workspace
from app.db.session import SessionFactory
from app.policy.actions import ApprovedAction, TargetAction, issue_approved_action
from app.sandbox.base import BuildResult, PushResult, PushTarget, SmokeEnv, SmokeResult
from app.targets.base import DeploySpec, EnvValue, TargetError

ACCOUNT = "881050425883"
SECRET_VALUE = "postgresql://app:hunter2@db:5432/app"  # noqa: S105 (test value)


def spec(run_id: uuid.UUID, connection_id: uuid.UUID, **overrides: Any) -> DeploySpec:
    values: dict[str, Any] = {
        "run_id": run_id,
        "connection_id": connection_id,
        "app": "legacy-flask-app",
        "source_image": "gc-build/legacy-flask-app:3f2a9c1b7d4e",
        "source_image_id": "sha256:" + "9c41e0d2b7a3" * 5 + "abcd",
        "smoke_passed": True,
        "tag": "3f2a9c1b7d4e",
        "port": 8000,
        "health_path": "/health",
        "env": [
            EnvValue(name="DATABASE_URL", value=SECRET_VALUE, secret=True),
            EnvValue(name="MODE", value="production"),
        ],
        "required_env": ["DATABASE_URL"],
    }
    values.update(overrides)
    return DeploySpec(**values)


class FakeSandbox:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.pushed: list[tuple[str, PushTarget]] = []

    async def build(
        self, context_dir: Path, dockerfile: Path, tag: str, *, run_id: str
    ) -> BuildResult:
        raise AssertionError("targets never build")

    async def run_smoke(
        self,
        image_ref: str,
        port: int,
        env: list[SmokeEnv],
        *,
        run_id: str,
        health_path: str | None = None,
    ) -> SmokeResult:
        raise AssertionError("targets never smoke-test")

    async def push(self, image_ref: str, target: PushTarget) -> PushResult:
        self.events.append(f"push {image_ref} -> {target.image}")
        self.pushed.append((image_ref, target))
        return PushResult(image=target.image, digest="sha256:" + "a" * 64, duration_ms=10)

    async def cleanup(self, run_id: str) -> None:
        return None


class FakeKubeApi:
    """Objects keyed by (kind, namespace, name). Deployments get a revision and a ready status
    when applied, the way the controller would."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.objects: dict[tuple[str, str | None, str], dict[str, Any]] = {}
        self.logs = "2026-10-12T10:00:00Z booting\n2026-10-12T10:00:01Z listening on 8000\n"
        self.ready = True
        self.pods: list[dict[str, Any]] = [
            {"metadata": {"name": "legacy-flask-app-abc"}, "status": {}}
        ]

    def put(self, kind: str, namespace: str | None, body: dict[str, Any]) -> None:
        self.objects[(kind, namespace, body["metadata"]["name"])] = body

    async def get(
        self, kind: str, name: str, namespace: str | None = None
    ) -> dict[str, Any] | None:
        # Like the real deployer: no Secrets without the RoleBinding in their namespace.
        if (
            kind == "secrets"
            and ("rolebindings", namespace, "groundcrew-deployer") not in self.objects
        ):
            raise TargetError(f"get secrets/{name} failed: 403 Forbidden")
        found = self.objects.get((kind, namespace, name))
        return copy.deepcopy(found) if found else None

    async def create(
        self, kind: str, body: dict[str, Any], namespace: str | None = None
    ) -> dict[str, Any]:
        self.events.append(f"create {kind}/{body['metadata']['name']}")
        self.put(kind, namespace, copy.deepcopy(body))
        return body

    async def apply(self, kind: str, body: dict[str, Any], namespace: str) -> dict[str, Any]:
        self.events.append(f"apply {kind}/{body['metadata']['name']}")
        body = copy.deepcopy(body)
        if kind == "deployments":
            old = self.objects.get((kind, namespace, body["metadata"]["name"]))
            revision = (
                int(
                    ((old or {}).get("metadata") or {})
                    .get("annotations", {})
                    .get("deployment.kubernetes.io/revision", "0")
                )
                + 1
            )
            self._ready(body, revision)
            self.objects[("replicasets", namespace, f"{body['metadata']['name']}-{revision}")] = {
                "metadata": {
                    "name": f"{body['metadata']['name']}-{revision}",
                    "annotations": {"deployment.kubernetes.io/revision": str(revision)},
                },
                "spec": {"template": copy.deepcopy(body["spec"]["template"])},
            }
        self.put(kind, namespace, body)
        return body

    def _ready(self, body: dict[str, Any], revision: int) -> None:
        body["metadata"]["annotations"] = {"deployment.kubernetes.io/revision": str(revision)}
        body["metadata"]["generation"] = revision
        want = body["spec"]["replicas"]
        body["status"] = {
            "observedGeneration": revision,
            "updatedReplicas": want,
            "availableReplicas": want if self.ready else 0,
        }

    async def merge_patch(
        self, kind: str, name: str, namespace: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        self.events.append(f"patch {kind}/{name}")
        current = self.objects[(kind, namespace, name)]
        current["spec"]["template"] = copy.deepcopy(body["spec"]["template"])
        revision = int(current["metadata"]["annotations"]["deployment.kubernetes.io/revision"]) + 1
        self._ready(current, revision)
        return current

    async def delete(self, kind: str, name: str, namespace: str | None = None) -> None:
        self.events.append(f"delete {kind}/{name}")
        self.objects.pop((kind, namespace, name), None)

    async def list(self, kind: str, namespace: str, label_selector: str) -> list[dict[str, Any]]:
        if kind == "pods":
            return copy.deepcopy(self.pods)
        return [
            copy.deepcopy(v)
            for (k, ns, _), v in self.objects.items()
            if k == kind and ns == namespace
        ]

    async def pod_log(
        self, namespace: str, pod: str, *, since_seconds: int, tail_lines: int
    ) -> str:
        return self.logs


def client_error(code: str, operation: str, message: str = "") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


class FakeAws:
    """A boto3.Session stand-in: `client(name)` returns an object whose methods are handled
    by `<service>__<operation>` methods here. Every call is recorded in `calls`."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.repositories: set[str] = set()
        self.log_groups: set[str] = set()
        self.security_groups: dict[str, str] = {}
        self.secrets: dict[str, str] = {}
        self.services: dict[str, dict[str, Any]] = {}
        self.revisions: dict[str, int] = {}
        self.task_definitions: dict[str, dict[str, Any]] = {}
        self.rollout = "COMPLETED"

    def client(self, name: str, region_name: str | None = None) -> Any:
        aws = self

        class Client:
            def __getattr__(self, operation: str) -> Any:
                def call(**kwargs: Any) -> Any:
                    aws.calls.append((name, operation, kwargs))
                    handler = getattr(aws, f"{name}__{operation}", None)
                    if handler is None:
                        raise AssertionError(f"unexpected AWS call {name}:{operation}")
                    return handler(**kwargs)

                return call

        return Client()

    def ops(self) -> list[str]:
        return [f"{s}:{o}" for s, o, _ in self.calls]

    def _write(self, what: str) -> None:
        self.events.append(what)

    # sts / ec2
    def sts__get_caller_identity(self) -> dict[str, Any]:
        return {"Account": ACCOUNT}

    def ec2__describe_vpcs(self, **_: Any) -> dict[str, Any]:
        return {"Vpcs": [{"VpcId": "vpc-1"}]}

    def ec2__describe_subnets(self, **_: Any) -> dict[str, Any]:
        return {"Subnets": [{"SubnetId": "subnet-a", "VpcId": "vpc-1"}]}

    def ec2__describe_security_groups(self, Filters: list[dict[str, Any]]) -> dict[str, Any]:  # noqa: N803
        name = Filters[0]["Values"][0]
        group = self.security_groups.get(name)
        return {"SecurityGroups": [{"GroupId": group}] if group else []}

    def ec2__create_security_group(self, **kwargs: Any) -> dict[str, Any]:
        self._write(f"create sg {kwargs['GroupName']}")
        self.security_groups[kwargs["GroupName"]] = "sg-1"
        return {"GroupId": "sg-1"}

    def ec2__authorize_security_group_ingress(self, **_: Any) -> dict[str, Any]:
        return {}

    def ec2__describe_network_interfaces(self, **_: Any) -> dict[str, Any]:
        return {"NetworkInterfaces": [{"Association": {"PublicIp": "203.0.113.9"}}]}

    # ecr
    def ecr__describe_repositories(self, repositoryNames: list[str]) -> dict[str, Any]:  # noqa: N803
        if repositoryNames[0] not in self.repositories:
            raise client_error("RepositoryNotFoundException", "DescribeRepositories")
        return {"repositories": [{"repositoryName": repositoryNames[0]}]}

    def ecr__create_repository(
        self,
        repositoryName: str,  # noqa: N803
        tags: list[dict[str, str]],
    ) -> dict[str, Any]:
        self._write(f"create ecr {repositoryName}")
        self.repositories.add(repositoryName)
        return {}

    def ecr__get_authorization_token(self) -> dict[str, Any]:
        token = base64.b64encode(b"AWS:registry-password").decode()
        endpoint = f"https://{ACCOUNT}.dkr.ecr.us-east-2.amazonaws.com"
        return {"authorizationData": [{"authorizationToken": token, "proxyEndpoint": endpoint}]}

    # logs
    def logs__describe_log_groups(self, logGroupNamePrefix: str) -> dict[str, Any]:  # noqa: N803
        return {
            "logGroups": [
                {"logGroupName": g} for g in self.log_groups if g.startswith(logGroupNamePrefix)
            ]
        }

    def logs__create_log_group(self, logGroupName: str, tags: dict[str, str]) -> dict[str, Any]:  # noqa: N803
        self._write(f"create log group {logGroupName}")
        self.log_groups.add(logGroupName)
        return {}

    def logs__put_retention_policy(self, **_: Any) -> dict[str, Any]:
        return {}

    def logs__filter_log_events(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "events": [
                {
                    "timestamp": 1_791_000_000_000,
                    "logStreamName": "legacy-flask-app/x/1",
                    "message": "listening on 8000\n",
                }
            ]
        }

    # secretsmanager
    def secretsmanager__describe_secret(self, SecretId: str) -> dict[str, Any]:  # noqa: N803
        if SecretId not in self.secrets:
            raise client_error("ResourceNotFoundException", "DescribeSecret")
        return {"ARN": f"arn:aws:secretsmanager:us-east-2:{ACCOUNT}:secret:{SecretId}-AbCdEf"}

    def secretsmanager__create_secret(
        self,
        Name: str,  # noqa: N803
        SecretString: str,  # noqa: N803
        **_: Any,
    ) -> dict[str, Any]:
        self._write(f"create secret {Name}")
        self.secrets[Name] = SecretString
        return {"ARN": f"arn:aws:secretsmanager:us-east-2:{ACCOUNT}:secret:{Name}-AbCdEf"}

    def secretsmanager__put_secret_value(self, SecretId: str, SecretString: str) -> dict[str, Any]:  # noqa: N803
        self._write(f"put secret {SecretId}")
        self.secrets[SecretId] = SecretString
        return {}

    # ecs
    def ecs__describe_clusters(self, clusters: list[str]) -> dict[str, Any]:
        return {"clusters": [{"clusterName": clusters[0], "status": "ACTIVE"}]}

    def _arn(self, service: str) -> str:
        return f"arn:aws:ecs:us-east-2:{ACCOUNT}:service/groundcrew-deploys/{service}"

    def ecs__describe_services(
        self, cluster: str, services: list[str], include: list[str]
    ) -> dict[str, Any]:
        out = []
        for name in services:
            name = name.rsplit("/", 1)[-1]
            if name in self.services:
                out.append(copy.deepcopy(self.services[name]))
        return {"services": out}

    def ecs__list_services(self, cluster: str) -> dict[str, Any]:
        return {"serviceArns": [self._arn(s) for s in self.services]}

    def ecs__register_task_definition(self, **kwargs: Any) -> dict[str, Any]:
        family = kwargs["family"]
        self.revisions[family] = self.revisions.get(family, 0) + 1
        arn = f"arn:aws:ecs:us-east-2:{ACCOUNT}:task-definition/{family}:{self.revisions[family]}"
        self._write(f"register {family}:{self.revisions[family]}")
        self.task_definitions[arn] = kwargs
        return {"taskDefinition": {"taskDefinitionArn": arn}}

    def ecs__describe_task_definition(self, taskDefinition: str) -> dict[str, Any]:  # noqa: N803
        return {"taskDefinition": self.task_definitions[taskDefinition]}

    def _service_state(self, name: str, task_definition: str, desired: int) -> dict[str, Any]:
        return {
            "serviceArn": self._arn(name),
            "serviceName": name,
            "status": "ACTIVE",
            "taskDefinition": task_definition,
            "desiredCount": desired,
            "runningCount": desired,
            "tags": [{"key": "project", "value": "groundcrew"}],
            "deployments": [
                {
                    "status": "PRIMARY",
                    "rolloutState": self.rollout,
                    "rolloutStateReason": "tasks failed to start"
                    if self.rollout == "FAILED"
                    else None,
                    "runningCount": desired,
                    "desiredCount": desired,
                }
            ],
        }

    def ecs__create_service(self, **kwargs: Any) -> dict[str, Any]:
        self._write(f"create service {kwargs['serviceName']}")
        state = self._service_state(
            kwargs["serviceName"], kwargs["taskDefinition"], kwargs["desiredCount"]
        )
        state["tags"] = kwargs["tags"]
        self.services[kwargs["serviceName"]] = state
        return {"service": {"serviceArn": state["serviceArn"]}}

    def ecs__update_service(self, service: str, **kwargs: Any) -> dict[str, Any]:
        self._write(f"update service {service}")
        current = self.services[service]
        if "taskDefinition" in kwargs:
            current["taskDefinition"] = kwargs["taskDefinition"]
        if "desiredCount" in kwargs:
            current["desiredCount"] = kwargs["desiredCount"]
        return {"service": {"serviceArn": current["serviceArn"]}}

    def ecs__delete_service(self, service: str, **_: Any) -> dict[str, Any]:
        self._write(f"delete service {service}")
        self.services.pop(service)
        return {}

    def ecs__list_tasks(self, **kwargs: Any) -> dict[str, Any]:
        return {"taskArns": [] if kwargs.get("desiredStatus") == "STOPPED" else ["task-1"]}

    def ecs__describe_tasks(self, **_: Any) -> dict[str, Any]:
        service = next(iter(self.services.values()))
        return {
            "tasks": [
                {
                    "taskArn": "task-1",
                    "lastStatus": "RUNNING",
                    "taskDefinitionArn": service["taskDefinition"],
                    "stoppedReason": "Essential container exited",
                    "attachments": [
                        {"details": [{"name": "networkInterfaceId", "value": "eni-1"}]}
                    ],
                }
            ]
        }


async def approved_action(
    sf: SessionFactory,
    workspace: Workspace,
    *,
    action: TargetAction,
    plan_hash: str,
    run: Run | None = None,
    policy: ToolPolicy | None = None,
    status: ApprovalStatus = ApprovalStatus.approved,
) -> tuple[Run, Approval, ApprovedAction | None]:
    """A run with an approval for `action` on `plan_hash`; the token when it is approved."""
    async with sf() as session:
        if run is None:
            run = Run(workspace_id=workspace.id, agent="deploy", goal="g", status=RunStatus.running)
            session.add(run)
            await session.flush()
        approval = Approval(
            run_id=run.id,
            workspace_id=workspace.id,
            tool_call_id=f"call-{uuid.uuid4()}",
            tool_name=action,
            policy=policy
            or (ToolPolicy.write if action == "target_apply" else ToolPolicy.emergency),
            arguments={"plan_hash": plan_hash},
            status=status,
        )
        session.add(approval)
        await session.commit()
        token = None
        if status == ApprovalStatus.approved:
            token = await issue_approved_action(
                session, approval, run_id=run.id, action=action, plan_hash=plan_hash
            )
        return run, approval, token
