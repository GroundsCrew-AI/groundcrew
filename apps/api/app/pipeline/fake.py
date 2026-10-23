"""Fake mode for the deploy pipeline (APP_ENV=dev and PIPELINE_FAKE=1 only): the real API,
worker, pipeline, approvals, tokens, and K8sTarget, with scripted model answers, a scripted
sandbox, and an in-memory Kubernetes API. Playwright runs the deploy flow on this in CI, where
there is no Token Factory key, no Docker daemon to build in, and no cluster.

What a fake run does: Scout (rules only), write_dockerfile, a build that fails on psycopg2,
fix_build, a build that passes, smoke, plan_deploy, the approval, apply into the in-memory
cluster, verify (rollout ready, /health 200), and the live URL. The source is always the
py-build-trap fixture, whatever repository was picked.
"""

import copy
import json
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.connectors.credentials import CredentialsProvider
from app.db.models import Connection
from app.db.session import SessionFactory
from app.graph.sink import GraphResourceSink
from app.llm.budget import RunBudget
from app.llm.client import LLMResult, Message, OnCall, ToolSchema
from app.llm.fake import FakeLLM, reply
from app.llm.router import Task
from app.llm.structured import Schema
from app.sandbox.base import BuildResult, PushResult, PushTarget, SmokeCheck, SmokeEnv, SmokeResult
from app.sandbox.logs import error_lines, failing_step
from app.targets.base import TargetError
from app.targets.k8s import K8sTarget


class InMemoryKubeApi:
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


NAIVE = (
    "FROM python:3.12-slim\nWORKDIR /app\nCOPY requirements.txt .\n"
    "RUN pip install --no-cache-dir -r requirements.txt\nCOPY . .\nEXPOSE 8000\n"
    'CMD ["gunicorn", "-b", "0.0.0.0:8000", "app:app"]\n'
)
FIXED = NAIVE.replace(
    "WORKDIR /app\n",
    "RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev libpq-dev "
    "&& rm -rf /var/lib/apt/lists/*\nWORKDIR /app\n",
)
PSYCOPG2_LOG = (
    "#8 [4/6] RUN pip install --no-cache-dir -r requirements.txt\n"
    "#8 9.13   Error: pg_config executable not found.\n"
    '#8 ERROR: process "/bin/sh -c pip install --no-cache-dir -r requirements.txt" did not '
    "complete successfully: exit code: 1\n"
)
ANSWERS: dict[Task, dict[str, Any]] = {
    Task.detect_app_spec: {},
    Task.write_dockerfile: {"dockerfile": NAIVE, "rationale": "Slim Python image, gunicorn."},
    Task.fix_build: {
        "dockerfile": FIXED,
        "rationale": "psycopg2 builds from source: add gcc and the Postgres headers.",
        "changed_lines": ["RUN apt-get update && apt-get install -y ... libpq-dev"],
        "fix_kind": "system_packages",
    },
    Task.plan_deploy: {
        "replicas": 1,
        "size": "small",
        "secret_env": ["DATABASE_URL"],
        "rationale": "One small replica for a demo.",
    },
}


class ScriptedLLM(FakeLLM):
    """Answers each task with its scripted JSON (see ANSWERS), through the real client."""

    async def chat(
        self,
        task: Task,
        messages: Sequence[Message],
        *,
        budget: RunBudget,
        tools: Sequence[ToolSchema] | None = None,
        tool_choice: Any = None,
        schema: Schema | None = None,
        escalate: bool = False,
        max_tokens: int | None = None,
        on_call: OnCall | None = None,
    ) -> LLMResult:
        self.script.append(
            reply(json.dumps(ANSWERS.get(task, {})), prompt_tokens=800, completion_tokens=200)
        )
        return await super().chat(
            task,
            messages,
            budget=budget,
            tools=tools,
            tool_choice=tool_choice,
            schema=schema,
            escalate=escalate,
            max_tokens=max_tokens,
            on_call=on_call,
        )


class ScriptedSandbox:
    """The first build of each run fails on psycopg2; later builds pass. Nothing runs."""

    def __init__(self) -> None:
        self.builds: dict[str, int] = {}
        self.pushed: list[str] = []

    async def build(
        self, context_dir: Path, dockerfile: Path, tag: str, *, run_id: str
    ) -> BuildResult:
        n = self.builds[run_id] = self.builds.get(run_id, 0) + 1
        if n == 1:
            return BuildResult(
                False,
                PSYCOPG2_LOG,
                None,
                1200,
                failing_step(PSYCOPG2_LOG),
                error_lines(PSYCOPG2_LOG),
            )
        return BuildResult(True, "", tag, 1500)

    async def run_smoke(
        self,
        image_ref: str,
        port: int,
        env: list[SmokeEnv],
        *,
        run_id: str,
        health_path: str | None = None,
    ) -> SmokeResult:
        return SmokeResult(True, [SmokeCheck(health_path or "/", True, 200, "HTTP 200")], 40)

    async def push(self, image_ref: str, target: PushTarget) -> PushResult:
        self.pushed.append(target.image)
        return PushResult(target.image, "sha256:" + "f" * 64, 30)

    async def push_to_groundcrew(
        self, image_ref: str, registry_url: str, repository: str, tag: str
    ) -> PushResult:
        image = f"{registry_url}/{repository}:{tag}"
        self.pushed.append(image)
        return PushResult(image, "sha256:" + "e" * 64, 30)

    async def image_id(self, image_ref: str) -> str:
        return "sha256:" + uuid.uuid5(uuid.NAMESPACE_URL, image_ref).hex * 2

    async def cleanup(self, run_id: str) -> None:
        self.builds.pop(run_id, None)


class _NoCredentials:
    def close(self) -> None:
        return None


async def _credentials(_session: Any, _workspace_id: uuid.UUID) -> CredentialsProvider:
    return _NoCredentials()  # type: ignore[return-value]


async def _no_sleep(_seconds: float) -> None:
    return None


def fake_deps(session_factory: SessionFactory, settings: Settings) -> Any:
    """PipelineDeps for fake mode. One in-memory cluster per worker process."""
    from app.pipeline.runner import FixtureCopy, PipelineDeps

    kube = InMemoryKubeApi([])
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))

    def target_for(s: Settings, credentials: CredentialsProvider, conn: Connection) -> K8sTarget:
        if conn.kind != "k8s":
            raise TargetError("Fake mode deploys to Kubernetes connections only.")
        cluster = str(conn.config.get("cluster") or conn.name)
        return K8sTarget(
            s,
            credentials,
            conn.id,
            cluster=cluster,
            api=kube,
            http=http,
            sleep=_no_sleep,
            session_factory=session_factory,
        )

    return PipelineDeps(
        settings=settings,
        llm=ScriptedLLM(settings=settings),
        sandbox=ScriptedSandbox(),
        sink=GraphResourceSink(session_factory),
        source_for=lambda request, conn: FixtureCopy("py-build-trap"),
        target_for=target_for,
        credentials_for=_credentials,
        builder_sleep=_no_sleep,
    )
