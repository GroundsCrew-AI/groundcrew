"""Kubernetes end to end: detection, evidence, emergency fixes, and verify for a Deployment.

The cluster is a FixtureKubeReader: Deployment shop/web at revision 3 (image web:1.5) whose
pods crash (CrashLoopBackOff after exit 1), revision 2 (web:1.4) before it. Medic should open
an incident from the watcher, see revision 3 as the deploy that started it, offer
`k8s_rollout_undo` back to revision 2 behind an approval, patch the Deployment's template to
revision 2's (without its pod-template-hash label) once approved, and resolve after two
healthy verify rounds.
"""

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.actions.base import EmergencyActionError
from app.actions.fake import FakeEmergencyActions
from app.actions.k8s import K8sEmergencyActions
from app.agents.medic.events import ChangeMarker, Signal
from app.agents.medic.jobs import investigate_incident_job, verify_incident_job
from app.agents.medic.schemas import RootCause
from app.connectors import kubernetes as kube
from app.connectors.kubernetes import FixtureKubeReader
from app.db.models import Connection, Resource, Workspace
from app.db.session import SessionFactory
from app.incidents import k8s_rules
from app.incidents.store import open_incident
from app.incidents.watcher import watch_once
from app.llm.fake import FakeLLM, reply
from app.pipeline.resume import resume_run_job
from app.policy.engine import Decision
from app.telemetry import k8s
from app.telemetry.collect import Evidence
from app.telemetry.evidence import line
from tests.test_connections_api import (  # noqa: F401 (fixtures)
    KUBECONFIG,
    Member,
    _fake_kube,
    alice,
    client,
    overrides,
    secrets_key,
)
from tests.test_medic import Harness, _decide, _now, _state, eid, settings
from tests.test_medic import use_deps as medic_deps

pytestmark = pytest.mark.anyio

use_deps = medic_deps
NS, NAME = "shop", "web"


def _iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def _template(image: str, rs_hash: str | None = None) -> dict[str, Any]:
    labels = {"app": NAME}
    if rs_hash:
        labels["pod-template-hash"] = rs_hash
    return {
        "metadata": {"labels": labels},
        "spec": {"containers": [{"name": NAME, "image": image}]},
    }


def cluster(now: datetime, *, healthy: bool = False) -> FixtureKubeReader:
    """shop/web: revision 3 crash-looping (healthy=False) or revision 4 (2's template) ready."""
    rev = 4 if healthy else 3
    deployment = {
        "metadata": {
            "name": NAME,
            "namespace": NS,
            "generation": rev,
            "annotations": {k8s.REVISION: str(rev)},
        },
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": NAME}},
            "template": _template("web:1.4" if healthy else "web:1.5"),
        },
        "status": {
            "observedGeneration": rev,
            "replicas": 2,
            "updatedReplicas": 2,
            "readyReplicas": 2 if healthy else 0,
            "availableReplicas": 2 if healthy else 0,
            "conditions": [],
        },
    }

    def rs(revision: int, image: str, age: timedelta) -> dict[str, Any]:
        return {
            "metadata": {
                "name": f"{NAME}-{revision}abc",
                "namespace": NS,
                "labels": {"app": NAME},
                "creationTimestamp": _iso(now - age),
                "annotations": {k8s.REVISION: str(revision)},
                "ownerReferences": [{"kind": "Deployment", "name": NAME}],
            },
            "spec": {"template": _template(image, f"{revision}abc")},
        }

    replicasets = [
        rs(2, "web:1.4", timedelta(days=2)),
        rs(3, "web:1.5", timedelta(minutes=12)),
    ]
    if healthy:
        replicasets.append(rs(4, "web:1.4", timedelta(minutes=2)))

    def pod(i: int) -> dict[str, Any]:
        container: dict[str, Any] = {"name": NAME, "image": "web:1.5", "restartCount": 4}
        if healthy:
            container = {"name": NAME, "image": "web:1.4", "ready": True, "restartCount": 0}
            container["state"] = {"running": {"startedAt": _iso(now - timedelta(minutes=2))}}
        else:
            container["ready"] = False
            container["state"] = {"waiting": {"reason": "CrashLoopBackOff"}}
            container["lastState"] = {
                "terminated": {
                    "exitCode": 1,
                    "reason": "Error",
                    "finishedAt": _iso(now - timedelta(minutes=1)),
                }
            }
        return {
            "metadata": {
                "name": f"{NAME}-{rev}abc-{i}",
                "namespace": NS,
                "labels": {"app": NAME},
                "creationTimestamp": _iso(now - timedelta(minutes=10)),
            },
            "status": {
                "phase": "Running",
                "startTime": _iso(now - timedelta(minutes=10)),
                "conditions": [{"type": "Ready", "status": "True" if healthy else "False"}],
                "containerStatuses": [container],
            },
        }

    events = [
        {
            "metadata": {"name": "web-3abc-0.17f", "namespace": NS},
            "type": "Warning",
            "reason": "BackOff",
            "message": "Back-off restarting failed container web",
            "count": 7,
            "lastTimestamp": _iso(now - timedelta(minutes=1)),
            "involvedObject": {"kind": "Pod", "name": f"{NAME}-3abc-0"},
        }
    ]
    quiet = [f"{_iso(now - timedelta(minutes=30 - m))} INFO request ok" for m in range(20)]
    errors = [
        f"{_iso(now - timedelta(minutes=4, seconds=-20 * i))} ERROR KeyError: 'PRICE_API'"
        for i in range(10)
    ]
    reader = FixtureKubeReader(
        {
            "deployments": [deployment],
            "replicasets": replicasets,
            "pods": [pod(0), pod(1)],
            "events": [] if healthy else events,
        },
        logs={
            f"{NS}/{NAME}-{rev}abc-0": "\n".join(quiet + ([] if healthy else errors)),
        },
    )
    reader.allowed = {("patch", "deployments"), ("list", "replicasets")}
    return reader


async def _graph(sf: SessionFactory, ws: uuid.UUID, *, operate: bool = True) -> dict[str, Any]:
    async with sf() as session:
        conn = Connection(
            workspace_id=ws,
            kind="k8s",
            name="kind",
            status="connected",
            config={"cluster": "kind", "operate": operate},
        )
        session.add(conn)
        await session.flush()
        web = Resource(
            workspace_id=ws,
            connection_id=conn.id,
            provider="k8s",
            type="k8s:deployment",
            name=NAME,
            external_id=f"k8s:{conn.id}:{NS}/deployments/{NAME}",
            region=NS,
            config={"namespace": NS, "replicas": 2},
        )
        session.add(web)
        await session.commit()
        return {"conn": conn.id, "web": web.id, "connection": conn}


# --- detection --------------------------------------------------------------------------------


async def test_crash_looping_pods_with_none_ready_are_down() -> None:
    now = _now()
    workload = await k8s.fetch_workload(cluster(now), NS, NAME)
    assert workload is not None
    (signal,) = k8s_rules.k8s_signals(uuid.uuid4(), workload, now)
    assert signal.rule == "k8s_pods_crashing" and signal.down
    assert signal.detail == "2 pods crashing: CrashLoopBackOff (last exit Error 1); 0/2 pods ready"
    healthy = await k8s.fetch_workload(cluster(now, healthy=True), NS, NAME)
    assert healthy is not None and k8s_rules.k8s_signals(uuid.uuid4(), healthy, now) == []


async def test_a_failed_rollout_and_unready_pods() -> None:
    now = _now()
    reader = cluster(now, healthy=True)
    dep = reader.items["deployments"][0]
    dep["status"]["readyReplicas"] = 1
    dep["status"]["conditions"] = [
        {
            "type": "Progressing",
            "status": "False",
            "reason": "ProgressDeadlineExceeded",
            "message": 'ReplicaSet "web-4abc" has timed out progressing.',
            "lastUpdateTime": _iso(now - timedelta(minutes=1)),
        }
    ]
    reader.items["pods"][1]["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
    reader.items["pods"][1]["status"]["containerStatuses"][0]["ready"] = False
    workload = await k8s.fetch_workload(reader, NS, NAME)
    assert workload is not None
    signals = {s.rule: s for s in k8s_rules.k8s_signals(uuid.uuid4(), workload, now)}
    assert set(signals) == {"k8s_rollout_failed", "health_check"}
    assert not signals["k8s_rollout_failed"].down  # one pod still serves
    assert "timed out progressing" in signals["k8s_rollout_failed"].detail
    assert signals["health_check"].detail.startswith("1 pods failing readiness checks")


async def test_the_pods_logs_feed_the_error_spike_rule() -> None:
    now = _now()
    signals = await k8s_rules.check_deployment(cluster(now), uuid.uuid4(), NS, NAME, now)
    assert {s.rule for s in signals} == {"k8s_pods_crashing", "log_error_spike"}


async def test_the_watcher_opens_an_incident_for_a_crashing_deployment(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    sf = session_factory
    ids = await _graph(sf, workspace.id)
    now = _now()
    opened = await watch_once(sf, kube_for=lambda conn: cluster(now), now=now)
    (result,) = opened
    assert result.incident.affected_resource_ids == [ids["web"]]
    # The errors in its logs came first (4 minutes ago), then the crash loop.
    assert result.incident.detector_rule == "log_error_spike"
    assert {s["rule"] for s in result.incident.signals} == {"log_error_spike", "k8s_pods_crashing"}
    assert result.incident.severity == "high"


# --- evidence ---------------------------------------------------------------------------------


async def test_evidence_names_the_revision_that_started_it() -> None:
    now = _now()
    out = await k8s.read_workload(cluster(now), NS, NAME, now - timedelta(minutes=30), now)
    (change,) = out.changes
    assert change.kind == "deploy" and change.ref == "shop/web revision 3"
    text = "\n".join(entry.message for entry in out.lines)
    assert "revision 3 rolled out: image web:1.5" in text
    assert "event BackOff on Pod web-3abc-0 (x7)" in text
    assert "CrashLoopBackOff" in text and "KeyError: 'PRICE_API'" in text
    assert "now: revision 3, 0/2 pods ready, image web:1.5" in text


# --- actions ----------------------------------------------------------------------------------


async def test_rollout_undo_needs_operate_and_patch_rights(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    sf = session_factory
    now = _now()
    off = await _graph(sf, workspace.id, operate=False)
    actions = K8sEmergencyActions(sf, settings(), operator_for=lambda c: cluster(now))
    with pytest.raises(EmergencyActionError, match="Emergency fixes are off for kind"):
        await actions.plan_k8s_rollout_undo(off["conn"], NS, NAME)
    on = await _graph(sf, workspace.id)
    denied = cluster(now)
    denied.allowed = set()
    refused = K8sEmergencyActions(sf, settings(), operator_for=lambda c: denied)
    with pytest.raises(EmergencyActionError, match="may not patch Deployments in shop"):
        await refused.plan_k8s_rollout_undo(on["conn"], NS, NAME)
    plan = await actions.plan_k8s_rollout_undo(on["conn"], NS, NAME)
    assert (plan.current, plan.desired) == ("shop/web revision 3", "shop/web revision 2")
    assert plan.summary == "Roll web back to revision 2 (web:1.4); now revision 3 (web:1.5)"


# --- Medic end to end -------------------------------------------------------------------------


def _evidence(web: uuid.UUID, now: datetime) -> tuple[Evidence, str, str]:
    ev = Evidence()
    key = f"k8s:{NAME}"
    ev.sources[key] = [
        line(now - timedelta(minutes=12), "k8s", NAME, "revision 3 rolled out: image web:1.5"),
        line(
            now - timedelta(minutes=1),
            "k8s",
            NAME,
            "pod web-3abc-0 container web: CrashLoopBackOff",
        ),
    ]
    ev.truncated[key] = False
    ev.source_resources[key] = web
    ev.changes = [
        ChangeMarker(
            kind="deploy",
            ref="shop/web revision 3",
            at=now - timedelta(minutes=12),
            label="web revision 3 (web:1.5)",
            resource_id=web,
        )
    ]
    return ev, eid(ev, key, "rolled out"), eid(ev, key, "CrashLoopBackOff")


async def test_medic_rolls_a_crashing_deployment_back_and_resolves(
    session_factory: SessionFactory, workspace: Workspace, use_deps: Any
) -> None:
    sf = session_factory
    ids = await _graph(sf, workspace.id)
    now = _now()
    live = {"cluster": cluster(now)}
    ev, rollout_line, crash_line = _evidence(ids["web"], now)
    root = {
        "summary": "Revision 3 (web:1.5) crashes on start: PRICE_API is missing.",
        "root_cause": "The web:1.5 image needs PRICE_API, which the Deployment does not set.",
        "claims": [
            {"text": "Revision 3 rolled out web:1.5.", "evidence_ids": [rollout_line]},
            {"text": "Its pods crash-loop.", "evidence_ids": [crash_line]},
        ],
        "triggering_change": {"kind": "deploy", "ref": "shop/web revision 3"},
        "affected_resources": [NAME],
        "confidence": "high",
        "fix_options": [{"kind": "k8s_rollout_undo", "description": "Back to revision 2."}],
    }
    RootCause.model_validate(root)
    llm = FakeLLM(reply(json.dumps(root)), settings=settings())
    k8s_actions = K8sEmergencyActions(sf, llm.settings, operator_for=lambda c: live["cluster"])
    h = Harness(sf, ev, llm, actions=FakeEmergencyActions(sf, kube=k8s_actions))
    h.deps.kube_for = lambda conn: live["cluster"]
    use_deps(h)
    async with sf() as session:
        opened = await open_incident(
            session,
            workspace_id=workspace.id,
            resource_ids=[ids["web"]],
            detected_by="watcher",
            signals=[
                Signal(
                    rule="k8s_pods_crashing",
                    resource_id=ids["web"],
                    resource_name=NAME,
                    detail="2 pods crashing: CrashLoopBackOff; 0/2 pods ready",
                    at=now - timedelta(minutes=2),
                    down=True,
                )
            ],
        )
    incident_id, run_id = opened.incident.id, opened.run.id
    await investigate_incident_job(sf, {"incident_id": str(incident_id), "run_id": str(run_id)})
    incident, state, _ = await _state(sf, incident_id)
    assert incident.status == "waiting_approval", state.error
    (fix,) = [f for f in state.fixes if f.fix.kind == "emergency"]
    assert fix.fix.action == "k8s_rollout_undo" and fix.fix.title == "Roll back web to revision 2"
    assert [(p.what, p.before, p.after) for p in fix.fix.plan_lines] == [
        ("web pod template", "revision 3", "revision 2")
    ]

    await _decide(sf, workspace, fix.approval_id, Decision.approve)
    await resume_run_job(sf, {"run_id": str(run_id), "approval_id": str(fix.approval_id)})
    (patched,) = live["cluster"].patches
    assert patched[:2] == (NS, NAME)
    template = patched[2][0]["value"]
    assert template["spec"]["containers"][0]["image"] == "web:1.4"
    assert template["metadata"]["labels"] == {"app": NAME}  # no pod-template-hash
    incident, state, _ = await _state(sf, incident_id)
    assert incident.status == "verifying"

    # The rollback rolls out as revision 4 and its pods are ready: two rounds resolve it.
    live["cluster"] = cluster(now, healthy=True)
    payload = {"incident_id": str(incident_id), "run_id": str(run_id)}
    await verify_incident_job(sf, payload)
    incident, state, _ = await _state(sf, incident_id)
    assert state.verify is not None and state.verify.ok, state.verify
    await verify_incident_job(sf, payload)
    incident, state, _ = await _state(sf, incident_id)
    assert incident.status == "resolved"


async def test_verify_waits_while_the_rollback_rolls_out(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    from app.agents.medic.verify import k8s_checks

    sf = session_factory
    ids = await _graph(sf, workspace.id)
    now = _now()
    reader = cluster(now, healthy=True)
    reader.items["deployments"][0]["status"]["updatedReplicas"] = 1
    async with sf() as session:
        web = await session.get_one(Resource, ids["web"])
    waits: list[str] = []
    checks = await k8s_checks(
        reader, web, since=now - timedelta(minutes=3), now=now, check_logs=True, waits=waits
    )
    assert waits == ["web rollout in progress: 1/2 pods updated"]
    assert {c.name: c.ok for c in checks} == {"rollout": True, "health": True, "errors": True}


# --- turning emergency fixes on ------------------------------------------------------------------


async def test_turning_on_emergency_fixes_checks_the_kubeconfigs_rights(
    client: httpx.AsyncClient,  # noqa: F811
    alice: Member,  # noqa: F811
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_kube(monkeypatch)
    made = await client.post(
        "/connections/k8s",
        json={"workspace_id": str(alice.workspace_id), "kubeconfig": KUBECONFIG},
        headers=alice.headers,
    )
    conn_id = made.json()["id"]
    operator = cluster(_now())
    operator.allowed = set()
    monkeypatch.setattr(kube, "live_operator", lambda text: operator)
    url = f"/connections/{conn_id}/k8s/operate"
    refused = await client.post(url, json={"enabled": True}, headers=alice.headers)
    assert refused.status_code == 422
    assert "may not patch Deployments" in refused.json()["detail"]
    operator.allowed = {("patch", "deployments"), ("list", "replicasets")}
    on = await client.post(url, json={"enabled": True}, headers=alice.headers)
    assert on.status_code == 200, on.text
    assert "operate" in on.json()["capabilities"]
    off = await client.post(url, json={"enabled": False}, headers=alice.headers)
    assert "operate" not in off.json()["capabilities"]
    async with session_factory() as session:
        conn = await session.scalar(select(Connection).where(Connection.id == uuid.UUID(conn_id)))
        assert conn is not None and conn.config["operate"] is False
