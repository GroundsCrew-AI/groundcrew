"""Medic (Phase 5 M3) end to end on fakes: the three checkout-demo scenarios from incident to
resolved, approvals in order with single-use tokens, verify, memory, and the failure paths.
FakeLLM, fake emergency actions, a mock GitHub and Tavily; no network."""

import base64
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.actions.fake import FakeEmergencyActions, FakeSecret, FakeService
from app.agents.medic import agent
from app.agents.medic import diagnose as dx
from app.agents.medic.events import ChangeMarker, Signal
from app.agents.medic.jobs import investigate_incident_job, verify_incident_job
from app.agents.medic.schemas import Claim, EvidenceItem, RootCause, TriggeringChange, evidence_id
from app.agents.medic.state import MedicState
from app.config import Settings
from app.connectors.aws import AwsReader
from app.db.models import (
    Approval,
    ApprovalStatus,
    Connection,
    Edge,
    Incident,
    Job,
    Resource,
    Run,
    RunStatus,
    RunStep,
    Workspace,
)
from app.db.session import SessionFactory
from app.incidents.followups import track_followups
from app.incidents.store import open_incident
from app.llm.fake import FakeLLM, reply
from app.pipeline.resume import resume_run_job
from app.policy.engine import ApprovalStateError, Decision, decide_approval
from app.telemetry.collect import Evidence
from app.telemetry.evidence import line
from app.tools.github_pr import (
    FakeFixPullRequests,
    FixPullRequestError,
    GitHubFixPullRequests,
    PullRequestDraft,
    check_draft,
)
from app.tools.tavily import TavilyClient, search_query
from tests.aws_fixture import ACCOUNT, REGION, fixture_session
from tests.discovery_helpers import INSTALLATION, Recorded, github_app, mock_transport
from tests.medic_fakes import approve_medic_action, medic_run

pytestmark = pytest.mark.anyio

REPO = "GroundsCrew-AI/checkout-demo"
TD = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition"
SECRET_ARN = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:checkout/database-url-AbC123"
BAD_SHA = "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432"
INDEX_SHA = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"
OOM = "OutOfMemoryError: Container killed due to memory usage"
GOOD_TASK = '{"cpu": "256", "memory": "512"}\n'
BAD_TASK = '{"cpu": "256", "memory": "512", "container_memory": 24}\n'


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "model_nano": "m/nano",
        "model_super": "m/super",
        "model_ultra": "m/ultra",
        "tavily_api_key": SecretStr("tvly-test-key-0123456789"),
        "medic_verify_interval_seconds": 30,
        **overrides,
    }
    return Settings(_env_file=None, **values)


# --- graph ------------------------------------------------------------------------------------


async def demo_graph(sf: SessionFactory, ws: uuid.UUID) -> dict[str, uuid.UUID]:
    async with sf() as session:
        aws = Connection(
            workspace_id=ws,
            kind="aws",
            name="demo",
            status="connected",
            config={"account_id": ACCOUNT},
        )
        gh = Connection(
            workspace_id=ws,
            kind="github",
            name="gh",
            status="connected",
            config={"installation_id": INSTALLATION},
        )
        session.add_all([aws, gh])
        await session.flush()

        def res(type_: str, name: str, external_id: str | None = None, **kw: Any) -> Resource:
            r = Resource(
                workspace_id=ws,
                connection_id=kw.pop("conn", aws.id),
                provider=kw.pop("provider", "aws"),
                type=type_,
                name=name,
                external_id=external_id or f"{type_}:{name}",
                region=REGION,
                **kw,
            )
            session.add(r)
            return r

        api = res("ecs:service", "checkout-api", config={"cluster_arn": "checkout"})
        worker = res("ecs:service", "checkout-worker", config={"cluster_arn": "checkout"})
        secret = res("secretsmanager:secret", "checkout/database-url", SECRET_ARN)
        db = res("rds:db", "checkout-orders-db")
        api_logs = res("logs:log-group", "/ecs/checkout-api")
        repo = res(
            "github:repository",
            REPO,
            provider="github",
            conn=gh.id,
            repo=REPO,
            config={"default_branch": "main"},
        )
        await session.flush()
        for src, dst, kind in (
            (api, secret, "uses"),
            (worker, secret, "uses"),
            (api, db, "reads"),
            (worker, db, "reads"),
            (api, api_logs, "logs_to"),
            (api, repo, "defined_in"),
            (worker, repo, "defined_in"),
        ):
            session.add(Edge(workspace_id=ws, from_id=src.id, to_id=dst.id, kind=kind))
        await session.commit()
        return {
            "api": api.id,
            "worker": worker.id,
            "secret": secret.id,
            "db": db.id,
            "repo": repo.id,
            "api_logs": api_logs.id,
            "aws": aws.id,
        }


# --- evidence per scenario ----------------------------------------------------------------------


def _evidence(
    sources: dict[str, list[tuple[float, str]]],
    ids: dict[str, uuid.UUID],
    changes: list[ChangeMarker],
    t0: datetime,
) -> Evidence:
    ev = Evidence()
    for key, rows in sources.items():
        resource = key.split(":", 1)[1]
        ev.sources[key] = [
            line(t0 + timedelta(minutes=m), key.split(":")[0], resource, msg) for m, msg in rows
        ]
        ev.truncated[key] = False
        name = resource.rsplit("/", 1)[-1].replace("checkout-demo", "repo")
        rid = {
            "checkout-worker": ids["worker"],
            "checkout-api": ids["api"],
            "database-url": ids["secret"],
            "checkout-orders-db": ids["db"],
            "repo": ids["repo"],
        }.get(name)
        if rid:
            ev.source_resources[key] = rid
    ev.changes = changes
    return ev


def eid(ev: Evidence, key: str, needle: str) -> str:
    item = next(x for x in ev.sources[key] if needle in x.message)
    return evidence_id(key, item.at.isoformat(), item.message)


def oom_evidence(ids: dict[str, uuid.UUID], t0: datetime) -> Evidence:
    return _evidence(
        {
            "ecs:checkout-worker": [
                (
                    -9,
                    "deployment ecs-svc/2 PRIMARY rollout FAILED "
                    "task definition checkout-worker:42",
                ),
                (-4, f"task aaa stopped: {OOM}; container worker exit code 137 ({OOM}) [OOM]"),
                (-1, f"task bbb stopped: {OOM}; container worker exit code 137 ({OOM}) [OOM]"),
            ],
            f"github:{REPO}": [
                (
                    -11,
                    f"commit {BAD_SHA[:7]} by dev: Tune worker memory "
                    "| files: worker/ecs-task.json",
                ),
                (-2, "workflow run deploy.yml #41 (push) completed failure at 9f8e7d6"),
            ],
        },
        ids,
        [
            ChangeMarker(
                kind="commit",
                ref=BAD_SHA,
                at=t0 - timedelta(minutes=11),
                label="9f8e7d6 Tune worker memory",
            )
        ],
        t0,
    )


def oom_root_cause(ev: Evidence) -> str:
    oom_id = eid(ev, "ecs:checkout-worker", "[OOM]")
    commit_id = eid(ev, f"github:{REPO}", "commit")
    return json.dumps(
        {
            "summary": "Worker tasks are OOM-killed since commit 9f8e7d6 set a 24 MiB limit.",
            "root_cause": "worker/ecs-task.json sets container_memory 24; tasks exit 137.",
            "claims": [
                {"text": "Worker tasks exit 137 (OOM).", "evidence_ids": [oom_id]},
                {
                    "text": "Commit 9f8e7d6 changed the worker task.",
                    "evidence_ids": [commit_id, "ev-made-up"],
                },
                {"text": "The database is slow.", "evidence_ids": ["ev-0000000000"]},
            ],
            "triggering_change": {
                "kind": "commit",
                "ref": BAD_SHA[:7],
                "at": None,
                "description": "Tune worker memory",
                "evidence_ids": [commit_id],
            },
            "affected_resources": ["checkout-worker"],
            "confidence": "high",
            "fix_options": [
                {
                    "kind": "ecs_rollback_service",
                    "description": "roll back",
                    "target": "checkout-worker",
                },
                {"kind": "pull_request", "description": "revert", "target": "worker/ecs-task.json"},
            ],
        }
    )


def oom_fix() -> str:
    return json.dumps(
        {
            "title": "Restore the worker's memory limit",
            "body": "Removes container_memory 24, which OOM-kills the worker.",
            "rationale": "The 24 MiB limit kills every worker task.",
            "files": [{"path": "worker/ecs-task.json", "content": GOOD_TASK}],
        }
    )


def github_routes(sha: str, files: dict[str, tuple[str, str | None]]) -> dict[str, Any]:
    """files: path -> (patch, current contents or None)."""
    routes: dict[str, Any] = {
        f"GET /repos/{REPO}/commits/{sha}": {
            "files": [
                {"filename": p, "status": "modified" if cur else "added", "patch": patch}
                for p, (patch, cur) in files.items()
            ]
        },
    }
    for path, (_, current) in files.items():
        if current is not None:
            routes[f"GET /repos/{REPO}/contents/{path}"] = {
                "encoding": "base64",
                "content": base64.b64encode(current.encode()).decode(),
                "sha": "blobsha",
            }
    return routes


def tavily(
    results: list[dict[str, str]] | None = None, recorded: Recorded | None = None
) -> TavilyClient:
    body = {
        "results": results
        if results is not None
        else [
            {
                "title": "ECS OOM exit 137",
                "url": "https://docs.aws.amazon.com/oom",
                "content": "Exit 137 means the container was killed for exceeding its memory "
                "limit (OutOfMemoryError).",
            },
        ]
    }
    return TavilyClient(settings(), transport=mock_transport({"POST /search": body}, recorded))


def healthy_aws(*services: str) -> dict[str, Any]:
    svc = {
        s: {
            "serviceName": s,
            "status": "ACTIVE",
            "runningCount": 1,
            "desiredCount": 1,
            "deployments": [{"status": "PRIMARY", "rolloutState": "COMPLETED"}],
            "events": [],
        }
        for s in services
    }
    return {
        "ecs:DescribeServices": lambda p: {"services": [svc[p["services"][0]]]},
        "ecs:ListTasks": {"taskArns": []},
        "logs:FilterLogEvents": {"events": []},
        "cloudwatch:GetMetricData": {"MetricDataResults": []},
    }


class Harness:
    def __init__(
        self,
        sf: SessionFactory,
        ev: Evidence,
        llm: FakeLLM,
        *,
        actions: FakeEmergencyActions | None,
        gh: dict[str, Any] | None = None,
        aws: dict[str, Any] | None = None,
        research: TavilyClient | None = None,
    ) -> None:
        self.sf, self.evidence, self.llm = sf, ev, llm
        self.actions = actions
        self.prs = FakeFixPullRequests(sf)
        self.collected: list[Any] = []
        self.aws = aws or healthy_aws("checkout-worker", "checkout-api")
        self.deps = agent.MedicDeps(
            settings=llm.settings,
            llm=llm,
            collect=self.collect,
            actions=actions,
            prs=self.prs,
            github=github_app(extra_routes=gh or {}),
            tavily=research,
            reader_for=lambda conn: AwsReader(fixture_session(self.aws), llm.settings),
        )

    async def collect(
        self, ws: uuid.UUID, ids: list[uuid.UUID], since: datetime, until: datetime
    ) -> Evidence:
        self.collected.append((ws, ids, since, until))
        return self.evidence


@pytest.fixture
def use_deps() -> Iterator[Any]:
    def install(h: Harness) -> None:
        agent.set_deps_factory(lambda sf: h.deps)

    yield install
    agent.set_deps_factory(None)


async def _open(
    sf: SessionFactory, ws: uuid.UUID, rid: uuid.UUID, rule: str, detail: str
) -> tuple[uuid.UUID, uuid.UUID]:
    async with sf() as session:
        opened = await open_incident(
            session,
            workspace_id=ws,
            resource_ids=[rid],
            detected_by="watcher",
            signals=[
                Signal(
                    rule=rule,
                    resource_id=rid,
                    resource_name="svc",
                    detail=detail,
                    at=_now() - timedelta(minutes=2),
                )
            ],
        )
    return opened.incident.id, opened.run.id


async def _state(sf: SessionFactory, incident_id: uuid.UUID) -> tuple[Incident, MedicState, Run]:
    async with sf() as session:
        incident = await session.get(Incident, incident_id)
        assert incident is not None
        run = await session.scalar(
            select(Run).where(Run.incident_id == incident_id).order_by(Run.created_at.desc())
        )
        assert run is not None
        return incident, MedicState.model_validate(incident.state), run


async def _kinds(sf: SessionFactory, run_id: uuid.UUID) -> list[str]:
    async with sf() as session:
        steps = await session.scalars(
            select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.seq)
        )
        return [s.kind for s in steps]


async def _decide(
    sf: SessionFactory, ws: Workspace, approval_id: uuid.UUID | None, decision: Decision
) -> None:
    assert approval_id is not None
    async with sf() as session:
        await decide_approval(session, approval_id, decision, ws.owner_id)


# --- bad-deploy-oom: rollback + PR, approvals in order, verify, memory ------------------------


async def test_bad_deploy_oom_end_to_end(
    session_factory: SessionFactory, workspace: Workspace, use_deps: Any
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    t0 = _now()
    ev = oom_evidence(ids, t0)
    llm = FakeLLM(reply(oom_root_cause(ev)), reply(oom_fix()), settings=settings())
    actions = FakeEmergencyActions(
        sf,
        services={
            "checkout/checkout-worker": FakeService(
                revisions=[f"{TD}/checkout-worker:41", f"{TD}/checkout-worker:42"]
            ),
        },
    )
    recorded = Recorded()
    h = Harness(
        sf,
        ev,
        llm,
        actions=actions,
        gh=github_routes(BAD_SHA, {"worker/ecs-task.json": ("+  container_memory: 24", BAD_TASK)}),
        research=tavily(recorded=recorded),
    )
    use_deps(h)
    incident_id, run_id = await _open(
        sf, workspace.id, ids["worker"], "ecs_tasks_stopped", "3 tasks exit 137"
    )
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})

    incident, state, run = await _state(sf, incident_id)
    assert incident.status == "waiting_approval" and run.status == RunStatus.waiting_approval
    assert await _kinds(sf, run_id) == [
        "incident_opened",
        "incident_updated",
        "evidence_collected",
        "memory_checked",
        "research_done",
        "llm",
        "diagnosis",
        "llm",
        "fix_proposed",
        "approval_requested",
        "approval_requested",
        "incident_updated",
    ]
    # Ultra for the diagnosis, Ultra for the fix (DEV_MODE_NO_ULTRA off in this test).
    assert llm.models == ["m/ultra", "m/ultra"]
    # The prompt cites by id and carries research as background only.
    prompt = llm.calls[0]["messages"][-1]["content"]
    assert "[ev-" in prompt and "Background from web research (not evidence)" in prompt
    assert recorded.requests[0].headers["Authorization"] == "Bearer tvly-test-key-0123456789"
    assert state.research[0].url == "https://docs.aws.amazon.com/oom"
    # Uncited and made-up ids are gone; the triggering commit is kept.
    rc = state.root_cause
    assert (
        rc is not None
        and len(rc.claims) == 2
        and rc.claims[1].evidence_ids == [eid(ev, f"github:{REPO}", "commit")]
    )
    assert any("uncited claim: The database is slow." in d for d in state.dropped)
    assert rc.triggering_change is not None and rc.triggering_change.ref == BAD_SHA[:7]
    rollback, pr = state.fixes
    assert rollback.fix.kind == "emergency" and rollback.fix.action == "ecs_rollback_service"
    assert [(p.before, p.after) for p in rollback.fix.plan_lines] == [
        ("checkout-worker:42", "checkout-worker:41")
    ]
    assert pr.fix.kind == "pr" and pr.draft is not None
    assert pr.draft.branch == f"groundcrew/fix-{str(incident_id)[:8]}" and pr.draft.base == "main"
    diff = pr.fix.pull_request.files[0].diff if pr.fix.pull_request else ""
    assert '-{"cpu": "256", "memory": "512", "container_memory": 24}' in diff

    # The PR is approved first: nothing runs, because the rollback (fix 1) is still pending.
    await _decide(sf, workspace, pr.approval_id, Decision.approve)
    await resume_run_job(sf, {"run_id": str(run_id), "approval_id": str(pr.approval_id)})
    incident, state, run = await _state(sf, incident_id)
    assert (
        run.status == RunStatus.waiting_approval and actions.executed == [] and h.prs.opened == []
    )

    await _decide(sf, workspace, rollback.approval_id, Decision.approve)
    await resume_run_job(sf, {"run_id": str(run_id), "approval_id": str(rollback.approval_id)})
    incident, state, run = await _state(sf, incident_id)
    assert incident.status == "verifying" and [f.status for f in state.fixes] == [
        "applied",
        "applied",
    ]
    assert actions.services["checkout/checkout-worker"].revisions[-1].endswith(":41")
    assert h.prs.opened[0].files == {"worker/ecs-task.json": GOOD_TASK}
    async with sf() as session:
        used = list(await session.scalars(select(Approval).where(Approval.run_id == run_id)))
        assert all(a.used_at is not None for a in used)
        assert await session.scalar(select(Job).where(Job.kind == agent.VERIFY_JOB)) is not None

    # Two healthy rounds resolve it.
    payload = {"incident_id": str(incident_id), "run_id": str(run_id)}
    await verify_incident_job(sf, payload)
    incident, state, run = await _state(sf, incident_id)
    assert incident.status == "verifying" and state.healthy_streak == 1
    await verify_incident_job(sf, payload)
    incident, state, run = await _state(sf, incident_id)
    assert incident.status == "resolved" and run.status == RunStatus.succeeded
    assert state.outcome is not None and state.outcome.result == "resolved"
    assert incident.root_cause and "container_memory 24" in incident.root_cause
    kinds = await _kinds(sf, run_id)
    # Resolved by the rollback while the fix PR is still open: a follow-up tracks it, and
    # memory does not take the incident until the PR is merged.
    assert kinds[-4:] == [
        "verify_result",
        "incident_updated",
        "incident_updated",
        "incident_resolved",
    ]
    assert kinds.count("fix_applied") == 2
    assert incident.remembered_at is None
    merged = github_app(
        extra_routes={
            f"GET /repos/{REPO}/pulls/1": {
                "merged": True,
                "state": "closed",
                "merged_at": "2026-10-14T10:00:00Z",
                "merge_commit_sha": "m" * 40,
            }
        }
    )
    assert await track_followups(sf, merged) == 1
    incident, state, run = await _state(sf, incident_id)
    assert incident.remembered_at is not None

    # The same scenario again: memory finds it, research is skipped, Super confirms.
    ev2 = oom_evidence(ids, _now())
    llm2 = FakeLLM(reply(oom_root_cause(ev2)), reply(oom_fix()), settings=settings())
    actions2 = FakeEmergencyActions(
        sf,
        services={
            "checkout/checkout-worker": FakeService(
                revisions=[f"{TD}/checkout-worker:43", f"{TD}/checkout-worker:44"]
            ),
        },
    )
    h2 = Harness(
        sf,
        ev2,
        llm2,
        actions=actions2,
        gh=github_routes(BAD_SHA, {"worker/ecs-task.json": ("+  container_memory: 24", BAD_TASK)}),
        research=tavily(),
    )
    use_deps(h2)
    second_id, second_run = await _open(
        sf, workspace.id, ids["worker"], "ecs_tasks_stopped", "2 tasks exit 137"
    )
    assert second_id != incident_id
    await investigate_incident_job(sf, {"incident_id": str(second_id), "run_id": str(second_run)})
    _, state2, _ = await _state(sf, second_id)
    assert state2.memory is not None and state2.memory.incident_id == incident_id
    assert state2.research == [] and state2.tavily_calls == 0
    assert llm2.models[0] == "m/super"  # confirm_root_cause
    assert "A similar past incident" in llm2.calls[0]["messages"][-1]["content"]


# --- broken-env: secret rollback, then restart its users; no PR ---------------------------------


def env_evidence(ids: dict[str, uuid.UUID], t0: datetime) -> Evidence:
    return _evidence(
        {
            "secrets:checkout/database-url": [
                (
                    -12,
                    "value last changed (inside the incident window); "
                    "versions: AWSCURRENT=bbbbbbbb, AWSPREVIOUS=aaaaaaaa",
                )
            ],
            "ecs:checkout-api": [(-6, "task new111 stopped: Task failed container health checks")],
            "cloudwatch_logs:/ecs/checkout-api": [
                (-10, "health check failed: database unreachable")
            ],
            f"github:{REPO}": [(5, "no commits or workflow runs in the incident window")],
        },
        ids,
        [
            ChangeMarker(
                kind="secret_change",
                ref=f"{SECRET_ARN}:v-bad",
                at=t0 - timedelta(minutes=12),
                label="checkout/database-url changed",
                resource_id=ids["secret"],
            )
        ],
        t0,
    )


async def test_broken_env_rolls_back_the_secret_then_restarts_its_users(
    session_factory: SessionFactory, workspace: Workspace, use_deps: Any
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    ev = env_evidence(ids, _now())
    secret_line = eid(ev, "secrets:checkout/database-url", "last changed")
    health_line = eid(ev, "cloudwatch_logs:/ecs/checkout-api", "unreachable")
    rc = json.dumps(
        {
            "summary": "The DATABASE_URL secret changed to an unreachable host; new tasks fail.",
            "root_cause": "checkout/database-url changed 12 minutes before; no commit.",
            "claims": [
                {"text": "Secret changed in the window.", "evidence_ids": [secret_line]},
                {"text": "The api cannot reach the database.", "evidence_ids": [health_line]},
            ],
            "triggering_change": {
                "kind": "secret_change",
                "ref": f"{SECRET_ARN}:v-bad",
                "evidence_ids": [secret_line],
            },
            "affected_resources": ["checkout-api"],
            "confidence": "high",
            "fix_options": [],
        }
    )
    llm = FakeLLM(reply(rc), settings=settings())
    actions = FakeEmergencyActions(
        sf,
        services={
            "checkout/checkout-api": FakeService(revisions=[f"{TD}/checkout-api:17"]),
            "checkout/checkout-worker": FakeService(revisions=[f"{TD}/checkout-worker:41"]),
        },
        secrets={
            SECRET_ARN: FakeSecret(versions={"v-good": ["AWSPREVIOUS"], "v-bad": ["AWSCURRENT"]})
        },
    )
    h = Harness(sf, ev, llm, actions=actions, research=None)
    use_deps(h)
    incident_id, run_id = await _open(
        sf, workspace.id, ids["api"], "health_check", "2 tasks failed health checks"
    )
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})
    _, state, _ = await _state(sf, incident_id)
    assert [f.fix.action for f in state.fixes] == [
        "secret_rollback_version",
        "force_redeploy",
        "force_redeploy",
    ]
    assert {f.fix.plan.params["service"] for f in state.fixes[1:] if f.fix.plan} == {
        "checkout-api",
        "checkout-worker",
    }
    assert len(llm.calls) == 1  # no PR: nothing in git changed
    assert state.research == [] and not state.research_queries
    assert [f.fix.depends_on for f in state.fixes] == [[], ["fix-1"], ["fix-1"]]
    # A restart cannot be approved before the rollback is applied (it would restart on the
    # bad secret); rejecting is always allowed.
    with pytest.raises(ApprovalStateError, match=r"Fix 1 .* must be applied before this one"):
        await _decide(sf, workspace, state.fixes[2].approval_id, Decision.approve)
    for fix in state.fixes:
        await _decide(sf, workspace, fix.approval_id, Decision.approve)
        await resume_run_job(sf, {"run_id": str(run_id), "approval_id": "x"})
    incident, state, _ = await _state(sf, incident_id)
    assert incident.status == "verifying"
    assert actions.secrets[SECRET_ARN].holding("AWSCURRENT") == "v-good"
    assert [p.action for p in actions.executed] == [
        "secret_rollback_version",
        "force_redeploy",
        "force_redeploy",
    ]


async def test_rejecting_the_secret_rollback_closes_the_restarts_without_running_them(
    session_factory: SessionFactory, workspace: Workspace, use_deps: Any
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    ev = env_evidence(ids, _now())
    secret_line = eid(ev, "secrets:checkout/database-url", "last changed")
    rc = json.dumps(
        {
            "summary": "The DATABASE_URL secret changed to an unreachable host.",
            "root_cause": "checkout/database-url changed; no commit.",
            "claims": [{"text": "Secret changed in the window.", "evidence_ids": [secret_line]}],
            "triggering_change": {
                "kind": "secret_change",
                "ref": f"{SECRET_ARN}:v-bad",
                "evidence_ids": [secret_line],
            },
            "affected_resources": ["checkout-api"],
            "confidence": "high",
            "fix_options": [],
        }
    )
    actions = FakeEmergencyActions(
        sf,
        services={
            "checkout/checkout-api": FakeService(revisions=[f"{TD}/checkout-api:17"]),
            "checkout/checkout-worker": FakeService(revisions=[f"{TD}/checkout-worker:41"]),
        },
        secrets={
            SECRET_ARN: FakeSecret(versions={"v-good": ["AWSPREVIOUS"], "v-bad": ["AWSCURRENT"]})
        },
    )
    use_deps(Harness(sf, ev, FakeLLM(reply(rc), settings=settings()), actions=actions))
    incident_id, run_id = await _open(
        sf, workspace.id, ids["api"], "health_check", "2 tasks failed health checks"
    )
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})
    _, state, _ = await _state(sf, incident_id)
    await _decide(sf, workspace, state.fixes[0].approval_id, Decision.reject)
    await resume_run_job(sf, {"run_id": str(run_id), "approval_id": "x"})
    incident, state, _ = await _state(sf, incident_id)
    assert actions.executed == []
    assert [f.status for f in state.fixes] == ["rejected", "rejected", "rejected"]
    assert state.fixes[1].applied is not None and state.fixes[1].applied.error == (
        "Not run: fix 1 (Roll back checkout/database-url to its previous version) was rejected."
    )
    assert incident.status == "failed"
    async with sf() as session:
        restart = await session.get(Approval, state.fixes[1].approval_id)
        assert restart is not None and restart.status == ApprovalStatus.rejected
        assert restart.decided_via == "Medic: fix 1 was rejected"


# --- missing-index: no emergency, a PR with a new migration -------------------------------------


async def test_missing_index_proposes_a_new_migration_and_no_emergency(
    session_factory: SessionFactory, workspace: Workspace, use_deps: Any
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    t0 = _now()
    ev = _evidence(
        {
            "rds:checkout-orders-db": [(-4, "CPUUtilization 88% (baseline 5.0%) [SPIKE]")],
            f"github:{REPO}": [
                (
                    -10,
                    f"commit {INDEX_SHA[:7]} by dev: Clean up order indexes "
                    "| files: api/migrations/versions/0002_drop_customer_email_index.py",
                )
            ],
        },
        ids,
        [
            ChangeMarker(
                kind="commit",
                ref=INDEX_SHA,
                at=t0 - timedelta(minutes=10),
                label="1a2b3c4 Clean up",
            )
        ],
        t0,
    )
    rc = json.dumps(
        {
            "summary": "A migration dropped ix_orders_customer_email; searches scan the table.",
            "root_cause": "Commit 1a2b3c4 dropped the index; RDS CPU spiked.",
            "claims": [
                {
                    "text": "CPU spiked.",
                    "evidence_ids": [eid(ev, "rds:checkout-orders-db", "SPIKE")],
                }
            ],
            "triggering_change": {
                "kind": "commit",
                "ref": INDEX_SHA[:7],
                "evidence_ids": [eid(ev, f"github:{REPO}", "commit")],
            },
            "affected_resources": ["checkout-orders-db"],
            "confidence": "high",
            "fix_options": [],
        }
    )
    migration = "api/migrations/versions/0003_recreate_customer_email_index.py"
    fix = json.dumps(
        {
            "title": "Recreate the customer_email index",
            "body": "Forward migration.",
            "rationale": "Restores the index concurrently.",
            "files": [
                {"path": migration, "content": "revision = '0003'\ndown_revision = '0002'\n"}
            ],
        }
    )
    llm = FakeLLM(reply(rc), reply(fix), settings=settings(dev_mode_no_ultra=True))
    dropped = "api/migrations/versions/0002_drop_customer_email_index.py"
    h = Harness(
        sf,
        ev,
        llm,
        actions=FakeEmergencyActions(sf),
        gh=github_routes(
            INDEX_SHA, {dropped: ("+op.execute('DROP INDEX ...')", "revision = '0002'\n")}
        ),
    )
    use_deps(h)
    incident_id, run_id = await _open(sf, workspace.id, ids["db"], "rds_cpu_latency", "CPU 88%")
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})
    _, state, _ = await _state(sf, incident_id)
    assert [f.fix.kind for f in state.fixes] == ["pr"]
    assert llm.models == ["m/super", "m/super"]  # DEV_MODE_NO_ULTRA
    view = state.fixes[0].fix.pull_request
    assert view is not None and view.files[0].path == migration
    assert view.files[0].diff.startswith("--- /dev/null")


# --- failure paths ------------------------------------------------------------------------------


async def test_rejecting_every_fix_fails_the_incident(
    session_factory: SessionFactory, workspace: Workspace, use_deps: Any
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    ev = oom_evidence(ids, _now())
    llm = FakeLLM(reply(oom_root_cause(ev)), reply(oom_fix()), settings=settings())
    h = Harness(
        sf,
        ev,
        llm,
        actions=None,
        gh=github_routes(BAD_SHA, {"worker/ecs-task.json": ("+x", BAD_TASK)}),
    )
    use_deps(h)
    incident_id, run_id = await _open(
        sf, workspace.id, ids["worker"], "ecs_tasks_stopped", "exit 137"
    )
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})
    _, state, _ = await _state(sf, incident_id)
    # No Operate role: only the PR, with a note.
    assert [f.fix.kind for f in state.fixes] == ["pr"] and any(
        "no Operate role" in n for n in state.notes
    )
    await _decide(sf, workspace, state.fixes[0].approval_id, Decision.reject)
    await resume_run_job(sf, {"run_id": str(run_id), "approval_id": "x"})
    incident, state, run = await _state(sf, incident_id)
    assert incident.status == "failed" and state.outcome is not None
    assert state.outcome.reason == "Every fix was rejected." and run.status == RunStatus.succeeded
    assert h.prs.opened == []


async def test_verify_gives_up_and_says_what_still_fails(
    session_factory: SessionFactory, workspace: Workspace, use_deps: Any
) -> None:
    sf = session_factory
    ids = await demo_graph(sf, workspace.id)
    ev = oom_evidence(ids, _now())
    llm = FakeLLM(
        reply(oom_root_cause(ev)),
        reply(oom_fix()),
        settings=settings(medic_verify_timeout_seconds=0, medic_pr_verify_timeout_seconds=0),
    )
    broken = {
        "ecs:DescribeServices": {
            "services": [
                {
                    "serviceName": "checkout-worker",
                    "runningCount": 0,
                    "desiredCount": 1,
                    "deployments": [{"status": "PRIMARY", "rolloutState": "FAILED"}],
                }
            ]
        },
        "ecs:ListTasks": {"taskArns": []},
        "logs:FilterLogEvents": {"events": []},
        "cloudwatch:GetMetricData": {"MetricDataResults": []},
    }
    h = Harness(
        sf,
        ev,
        llm,
        actions=None,
        aws=broken,
        gh=github_routes(BAD_SHA, {"worker/ecs-task.json": ("+x", BAD_TASK)}),
    )
    use_deps(h)
    incident_id, run_id = await _open(
        sf, workspace.id, ids["worker"], "ecs_tasks_stopped", "exit 137"
    )
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})
    _, state, _ = await _state(sf, incident_id)
    await _decide(sf, workspace, state.fixes[0].approval_id, Decision.approve)
    await resume_run_job(sf, {"run_id": str(run_id), "approval_id": "x"})
    await verify_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})
    incident, state, _ = await _state(sf, incident_id)
    assert incident.status == "failed" and state.outcome is not None
    assert state.outcome.still_failing == ["checkout-worker: 0/1 tasks running, rollout FAILED"]


async def test_medic_without_token_factory_fails_cleanly(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    ids = await demo_graph(session_factory, workspace.id)

    def broken(_: SessionFactory) -> agent.MedicDeps:
        raise RuntimeError("TOKEN_FACTORY_BASE_URL and TOKEN_FACTORY_API_KEY must be set.")

    agent.set_deps_factory(broken)
    try:
        incident_id, run_id = await _open(
            session_factory, workspace.id, ids["api"], "health_check", "x"
        )
        await investigate_incident_job(
            session_factory, {"incident_id": str(incident_id), "run_id": str(run_id)}
        )
    finally:
        agent.set_deps_factory(None)
    incident, state, run = await _state(session_factory, incident_id)
    assert incident.status == "failed" and run.status == RunStatus.failed
    assert state.error is not None and "Medic cannot start" in state.error


# --- units ----------------------------------------------------------------------------------------


def test_check_keeps_only_cited_claims_and_known_changes() -> None:
    items = [EvidenceItem(id="ev-aaaaaaaaaa", source="ecs:x", timestamp="t", line="OOM")]
    changes = [ChangeMarker(kind="commit", ref=BAD_SHA, at=_now(), label="c")]
    rc = RootCause(
        summary="s",
        root_cause="r",
        confidence="high",
        claims=[
            Claim(text="cited", evidence_ids=["ev-aaaaaaaaaa", "ev-bogus"]),
            Claim(text="uncited", evidence_ids=["ev-bogus"]),
        ],
        triggering_change=TriggeringChange(kind="commit", ref=BAD_SHA[:7]),
    )
    checked, dropped = dx.check(rc, items, changes)
    assert [c.text for c in checked.claims] == ["cited"] and checked.claims[0].evidence_ids == [
        "ev-aaaaaaaaaa"
    ]
    assert checked.triggering_change is not None  # no ids, but it matches a collected commit
    invented = rc.model_copy(
        update={"triggering_change": TriggeringChange(kind="deploy", ref="nope"), "claims": []}
    )
    checked, dropped = dx.check(invented, items, changes)
    assert checked.triggering_change is None and checked.confidence == "low"
    assert "uncited triggering change: deploy nope" in dropped


def test_search_queries_drop_ids_and_numbers() -> None:
    text = (
        "2026-10-05T10:00:00+00:00 | ecs | checkout-worker "
        "| task 3f2a9c1b stopped: OutOfMemoryError exit code 137"
    )
    assert search_query(text) == "task stopped: OutOfMemoryError exit code"


def test_fix_drafts_refuse_secrets_workflows_and_escapes() -> None:
    base = {
        "installation_id": 1,
        "repo": REPO,
        "base": "main",
        "branch": "groundcrew/fix-abc",
        "title": "t",
        "body": "b",
    }
    for path in (
        ".env",
        "config/.env.production",
        ".github/workflows/deploy.yml",
        "../x",
        "/etc/x",
        "k.pem",
    ):
        with pytest.raises(FixPullRequestError):
            check_draft(PullRequestDraft(**base, files={path: "x"}))
    check_draft(PullRequestDraft(**base, files={"worker/ecs-task.json": GOOD_TASK}))


async def test_github_fix_pr_writes_only_its_branch_and_spends_the_token(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    recorded = Recorded()
    routes = {
        f"GET /repos/{REPO}/git/ref/heads/main": {"object": {"sha": "base123"}},
        f"POST /repos/{REPO}/git/refs": {"ref": "refs/heads/groundcrew/fix-abc"},
        f"PUT /repos/{REPO}/contents/worker/ecs-task.json": {"commit": {"sha": "head456"}},
        f"POST /repos/{REPO}/pulls": {"number": 7, "html_url": f"https://github.com/{REPO}/pull/7"},
    }
    app = github_app(recorded)
    prs = GitHubFixPullRequests(session_factory, app, transport=mock_transport(routes, recorded))
    draft = PullRequestDraft(
        installation_id=INSTALLATION,
        repo=REPO,
        base="main",
        branch="groundcrew/fix-abc",
        title="Restore memory",
        body="b",
        files={"worker/ecs-task.json": GOOD_TASK},
    )
    run = await medic_run(session_factory, workspace)
    token = await approve_medic_action(
        session_factory, workspace, run, action="open_fix_pr", plan_hash=draft.plan_hash()
    )
    ref = await prs.open_fix_pr(draft, token, run_id=run.id)
    assert (ref.number, ref.head_sha, ref.state) == (7, "head456", "open")
    writes = [
        r for r in recorded.requests if r.method != "GET" and "access_tokens" not in r.url.path
    ]
    assert [f"{r.method} {r.url.path}" for r in writes] == [
        f"POST /repos/{REPO}/git/refs",
        f"PUT /repos/{REPO}/contents/worker/ecs-task.json",
        f"POST /repos/{REPO}/pulls",
    ]
    assert json.loads(writes[0].content)["ref"] == "refs/heads/groundcrew/fix-abc"
    put = json.loads(writes[1].content)
    assert (
        put["branch"] == "groundcrew/fix-abc"
        and base64.b64decode(put["content"]).decode() == GOOD_TASK
    )
    assert json.loads(writes[2].content)["head"] == "groundcrew/fix-abc"
    with pytest.raises(Exception, match="already used"):
        await prs.open_fix_pr(draft, token, run_id=run.id)
    async with session_factory() as session:
        approval = await session.get(Approval, token.approval_id)
        assert (
            approval is not None and approval.status == ApprovalStatus.approved and approval.used_at
        )


async def test_ranking_keeps_every_source_and_puts_strong_signals_first() -> None:
    from app.agents.medic import evidence as ranking
    from app.llm.budget import MemoryUsageStore, RunBudget

    t0 = _now()
    noise = [(-30 + i * 0.1, f"poll ok {i}") for i in range(200)]
    ev = _evidence(
        {
            "cloudwatch_logs:/ecs/checkout-worker": noise,
            "ecs:checkout-worker": [
                (-1, f"task a stopped: {OOM}; container worker exit code 137 [OOM]")
            ],
            "secrets:checkout/database-url": [(-50, "last accessed (day resolution)")],
        },
        {
            "worker": uuid.uuid4(),
            "api": uuid.uuid4(),
            "secret": uuid.uuid4(),
            "db": uuid.uuid4(),
            "repo": uuid.uuid4(),
        },
        [],
        t0,
    )
    llm = FakeLLM(settings=settings())  # no embedding price: the heuristic decides
    budget = RunBudget(usage=MemoryUsageStore(), settings=settings())
    top, used = await ranking.rank(ranking.to_items(ev), "worker OOM", set(), llm, budget, limit=10)
    assert not used and len(top) == 10
    sources = {i.source for i in top}
    assert sources == {
        "cloudwatch_logs:/ecs/checkout-worker",
        "ecs:checkout-worker",
        "secrets:checkout/database-url",
    }
    assert [i.timestamp for i in top] == sorted(i.timestamp for i in top)


@pytest.mark.parametrize(
    ("ref", "marker", "same"),
    [
        ("9f8e7d6", "9f8e7d6a1b2c3d4e5f60718293a4b5c6d7e8f901", True),  # commit SHA prefix
        (
            "cd5bcca5-2e1b-4c0f-90c6-05df3e893b85",
            "arn:aws:secretsmanager:us-east-2:1:secret:db-AbC:cd5bcca5-2e1b-4c0f-90c6-05df3e893b85",
            True,
        ),  # a secret's bare version id
        ("checkout-api:30", "arn:aws:ecs:us-east-2:1:task-definition/checkout-api:30", True),
        (
            "arn:aws:secretsmanager:us-east-2:1:secret:db",
            "arn:aws:secretsmanager:us-east-2:1:secret:db-AbC:v1",
            True,
        ),
        ("main", "refs/heads/main", False),  # short and generic
        ("30", "arn:aws:ecs:us-east-2:1:task-definition/checkout-api:30", False),
        ("api:30", "arn:aws:ecs:us-east-2:1:task-definition/checkout-api:30", False),
    ],
)
def test_a_triggering_change_matches_a_collected_marker(ref: str, marker: str, same: bool) -> None:
    assert dx._same_change(ref, marker) is same


async def test_research_stops_at_the_workspace_daily_quota(
    session_factory: SessionFactory, workspace: Workspace, use_deps: Any
) -> None:
    sf = session_factory
    async with sf() as session:
        ws = await session.get_one(Workspace, workspace.id)
        ws.settings = {**(ws.settings or {}), "quotas": {"tavily": 0}}
        await session.commit()
    ids = await demo_graph(sf, workspace.id)
    ev = oom_evidence(ids, _now())
    recorded = Recorded()
    h = Harness(
        sf,
        ev,
        FakeLLM(reply(oom_root_cause(ev)), reply(oom_fix()), settings=settings()),
        actions=FakeEmergencyActions(sf, services={}),
        gh=github_routes(BAD_SHA, {"worker/ecs-task.json": ("+  container_memory: 24", BAD_TASK)}),
        research=tavily(recorded=recorded),
    )
    use_deps(h)
    incident_id, run_id = await _open(
        sf, workspace.id, ids["worker"], "ecs_tasks_stopped", "3 tasks exit 137"
    )
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})
    _, state, _ = await _state(sf, incident_id)
    assert state.research == [] and state.tavily_calls == 0
    assert recorded.requests == []
    async with sf() as session:
        step = await session.scalar(
            select(RunStep).where(RunStep.run_id == run_id, RunStep.kind == "research_done")
        )
    assert step is not None and "limit of 0 web research calls" in str(step.data)
