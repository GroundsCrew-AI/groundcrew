"""Kubernetes StatefulSets end to end, and ConfigMap / Secret changes as causes.

The cluster is a FixtureKubeReader: StatefulSet shop/db rolling out revision 3 (image db:1.5)
whose pods crash, stuck 12 minutes after the new ControllerRevision appeared (currentRevision is
still revision 2, db:1.4). Medic should open an incident from the watcher, see revision 3 as the
deploy that started it, offer `k8s_rollout_undo` back to revision 2 (kind statefulset) behind an
approval, patch the StatefulSet's template to revision 2's ControllerRevision template once
approved, and resolve after two healthy verify rounds.

ConfigMaps and Secrets are read as metadata only: the fixture holds values that must never come
back, and the live reader is checked to ask for a PartialObjectMetadataList.
"""

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest

from app.actions.base import ActionPlan, EmergencyActionError, k8s_params, k8s_workload
from app.actions.fake import FakeEmergencyActions
from app.actions.k8s import K8sEmergencyActions
from app.agents.medic import fixes as fx
from app.agents.medic.events import ChangeMarker, Signal
from app.agents.medic.jobs import investigate_incident_job, verify_incident_job
from app.agents.medic.schemas import RootCause
from app.agents.medic.verify import k8s_checks
from app.connectors import kubernetes as kube
from app.connectors.kubernetes import (
    METADATA_ACCEPT,
    FixtureKubeReader,
    KubeReadRefusedError,
    LiveKubeReader,
)
from app.db.models import Connection, Resource, Workspace
from app.db.session import SessionFactory
from app.discovery.types import ConnectorError
from app.incidents import k8s_rules
from app.incidents.store import open_incident
from app.incidents.watcher import watch_once
from app.llm.fake import FakeLLM, reply
from app.pipeline.resume import resume_run_job
from app.policy.engine import Decision
from app.telemetry import k8s
from app.telemetry.collect import Evidence
from app.telemetry.evidence import line
from tests.test_k8s_incidents import cluster as deployment_cluster
from tests.test_medic import Harness, _decide, _now, _state, eid, settings
from tests.test_medic import use_deps as medic_deps

pytestmark = pytest.mark.anyio

use_deps = medic_deps
NS, NAME = "shop", "db"
SECRET_VALUE = "hunter2-do-not-leak"  # noqa: S105 (a fixture value that must never come back)


def _iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def _template(image: str) -> dict[str, Any]:
    return {
        "metadata": {"labels": {"app": NAME}},
        "spec": {"containers": [{"name": NAME, "image": image}]},
    }


def cluster(now: datetime, *, healthy: bool = False, fresh: bool = False) -> FixtureKubeReader:
    """shop/db: revision 3 (db:1.5) stuck with crashing pods, or (healthy) revision 4 with
    revision 2's template, rolled out and ready. `fresh`: revision 3 appeared 2 minutes ago."""
    rev = 4 if healthy else 3

    def cr(revision: int, image: str, age: timedelta) -> dict[str, Any]:
        template = _template(image)
        # The StatefulSet controller stores the template as a patch with this marker.
        template["$patch"] = "replace"
        return {
            "apiVersion": "apps/v1",
            "kind": "ControllerRevision",
            "metadata": {
                "name": f"{NAME}-rev{revision}",
                "namespace": NS,
                "labels": {"app": NAME},
                "creationTimestamp": _iso(now - age),
                "ownerReferences": [{"kind": "StatefulSet", "name": NAME}],
            },
            "revision": revision,
            "data": {"spec": {"template": template}},
        }

    revisions = [
        cr(2, "db:1.4", timedelta(days=2)),
        cr(3, "db:1.5", timedelta(minutes=2 if fresh else 12)),
    ]
    if healthy:
        revisions.append(cr(4, "db:1.4", timedelta(minutes=2)))
    statefulset = {
        "metadata": {"name": NAME, "namespace": NS, "generation": rev},
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": NAME}},
            "template": _template("db:1.4" if healthy else "db:1.5"),
        },
        "status": {
            "observedGeneration": rev,
            "replicas": 2,
            "readyReplicas": 2 if healthy else 0,
            "updatedReplicas": 2 if healthy else 1,
            "currentRevision": f"{NAME}-rev4" if healthy else f"{NAME}-rev2",
            "updateRevision": f"{NAME}-rev{rev}",
        },
    }

    def pod(i: int) -> dict[str, Any]:
        container: dict[str, Any] = {"name": NAME, "image": "db:1.5", "restartCount": 3}
        if healthy:
            container = {"name": NAME, "image": "db:1.4", "ready": True, "restartCount": 0}
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
                "name": f"{NAME}-{i}",
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

    reader = FixtureKubeReader(
        {
            "statefulsets": [statefulset],
            "controllerrevisions": revisions,
            "pods": [pod(0), pod(1)],
            "events": [],
        }
    )
    reader.allowed = {("patch", "statefulsets"), ("list", "controllerrevisions")}
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
        db = Resource(
            workspace_id=ws,
            connection_id=conn.id,
            provider="k8s",
            type="k8s:statefulset",
            name=NAME,
            external_id=f"k8s:{conn.id}:{NS}/statefulsets/{NAME}",
            region=NS,
            config={"namespace": NS, "replicas": 2},
        )
        session.add(db)
        await session.commit()
        return {"conn": conn.id, "db": db.id}


# --- detection --------------------------------------------------------------------------------


async def test_a_stuck_statefulset_rollout_with_crashing_pods_is_down() -> None:
    now = _now()
    workload = await k8s.fetch_workload(cluster(now), NS, NAME, k8s.STATEFULSET)
    assert workload is not None and [k8s.revision(r) for r in workload.revisions] == [2, 3]
    assert k8s.current_revision(workload) == 3
    signals = {s.rule: s for s in k8s_rules.k8s_signals(uuid.uuid4(), workload, now)}
    assert set(signals) == {"k8s_rollout_failed", "k8s_pods_crashing"}
    assert all(s.down for s in signals.values())
    assert signals["k8s_rollout_failed"].detail == (
        "rollout stuck: 1/2 pods on revision 3 after 12 min, pods failing (0/2 pods ready)"
    )
    # A rollout that just started is not stuck yet; a healthy one has no signal at all.
    fresh = await k8s.fetch_workload(cluster(now, fresh=True), NS, NAME, k8s.STATEFULSET)
    assert fresh is not None
    rules = {s.rule for s in k8s_rules.k8s_signals(uuid.uuid4(), fresh, now)}
    assert rules == {"k8s_pods_crashing"}
    healthy = await k8s.fetch_workload(cluster(now, healthy=True), NS, NAME, k8s.STATEFULSET)
    assert healthy is not None and k8s_rules.k8s_signals(uuid.uuid4(), healthy, now) == []


async def test_unready_statefulset_pods_and_their_logs() -> None:
    now = _now()
    reader = cluster(now, healthy=True)
    reader.items["statefulsets"][0]["status"]["readyReplicas"] = 1
    reader.items["pods"][1]["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
    errors = [
        f"{_iso(now - timedelta(minutes=4, seconds=-20 * i))} ERROR connection refused"
        for i in range(10)
    ]
    quiet = [f"{_iso(now - timedelta(minutes=30 - m))} INFO ok" for m in range(20)]
    reader.logs = {f"{NS}/{NAME}-0": "\n".join(quiet + errors)}
    signals = await k8s_rules.check_workload(reader, uuid.uuid4(), NS, NAME, now, k8s.STATEFULSET)
    assert {s.rule for s in signals} == {"health_check", "log_error_spike"}
    (unready,) = [s for s in signals if s.rule == "health_check"]
    assert not unready.down  # one pod still serves
    assert unready.detail == "1 pods failing readiness checks (1/2 pods ready, not healthy)"


async def test_the_watcher_opens_an_incident_for_a_crashing_statefulset(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    sf = session_factory
    ids = await _graph(sf, workspace.id)
    now = _now()
    (result,) = await watch_once(sf, kube_for=lambda conn: cluster(now), now=now)
    assert result.incident.affected_resource_ids == [ids["db"]]
    assert {s["rule"] for s in result.incident.signals} == {
        "k8s_rollout_failed",
        "k8s_pods_crashing",
    }


# --- evidence ---------------------------------------------------------------------------------


async def test_evidence_has_controller_revisions_as_deploy_markers() -> None:
    now = _now()
    reader = cluster(now)
    out = await k8s.read_workload(
        reader, NS, NAME, now - timedelta(minutes=30), now, k8s.STATEFULSET
    )
    (change,) = out.changes
    assert change.kind == "deploy" and change.ref == "shop/db revision 3"
    assert change.label == "db revision 3 (db:1.5)"
    text = "\n".join(entry.message for entry in out.lines)
    assert "revision 3 rolled out: image db:1.5" in text
    assert "rollout stuck: 1/2 pods on revision 3" in text
    assert "now: revision 3, 0/2 pods ready, image db:1.5" in text
    assert "controllerrevisions" in reader.calls and "replicasets" not in reader.calls


# --- actions ----------------------------------------------------------------------------------


async def test_rollout_undo_plan_for_a_statefulset(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    sf = session_factory
    now = _now()
    ids = await _graph(sf, workspace.id)
    denied = cluster(now)
    denied.allowed = {("patch", "statefulsets")}
    refused = K8sEmergencyActions(sf, settings(), operator_for=lambda c: denied)
    with pytest.raises(EmergencyActionError, match="may not list ControllerRevisions in shop"):
        await refused.plan_k8s_rollout_undo(ids["conn"], NS, NAME, "statefulset")
    denied.allowed = set()
    with pytest.raises(EmergencyActionError, match="may not patch StatefulSets in shop"):
        await refused.plan_k8s_rollout_undo(ids["conn"], NS, NAME, "statefulset")

    actions = K8sEmergencyActions(sf, settings(), operator_for=lambda c: cluster(now))
    plan = await actions.plan_k8s_rollout_undo(ids["conn"], NS, NAME, "statefulset")
    assert plan.params == {"namespace": NS, "statefulset": NAME, "kind": "statefulset"}
    assert (plan.current, plan.desired) == ("shop/db revision 3", "shop/db revision 2")
    assert plan.summary == "Roll db back to revision 2 (db:1.4); now revision 3 (db:1.5)"
    assert k8s_workload(plan.params) == ("statefulset", NS, NAME)
    assert fx.plan_lines(plan)[0].what == "db pod template"
    restart = await actions.plan_k8s_rollout_restart(ids["conn"], NS, NAME, "statefulset")
    assert restart.current == "shop/db revision 3" and restart.desired is None


def test_deployment_params_and_plan_hashes_are_unchanged() -> None:
    """A Deployment's params carry no `kind`, so plans approved before StatefulSets keep their
    hash."""
    assert k8s_params(NS, "web") == {"namespace": NS, "deployment": "web"}
    assert k8s_workload({"namespace": NS, "deployment": "web"}) == ("deployment", NS, "web")
    plan = ActionPlan(
        action="k8s_rollout_undo",
        connection_id=uuid.UUID(int=1),
        target=f"{NS}/web",
        params={"namespace": NS, "deployment": "web"},
        current="shop/web revision 3",
        desired="shop/web revision 2",
        summary="-",
        planned_at=_now(),
    )
    assert plan.params == k8s_params(NS, "web")
    with pytest.raises(EmergencyActionError):
        k8s_params(NS, "web", "daemonset")


def test_a_controller_revision_template_loses_its_patch_marker() -> None:
    from app.actions.k8s import rollback_template

    (old, _) = cluster(_now()).items["controllerrevisions"]
    template = rollback_template(old)
    assert "$patch" not in template
    assert template == _template("db:1.4")
    assert "$patch" in old["data"]["spec"]["template"]  # the cluster's object is untouched


# --- verify -----------------------------------------------------------------------------------


async def test_verify_needs_the_statefulset_rollout_complete(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    sf = session_factory
    ids = await _graph(sf, workspace.id)
    now = _now()
    async with sf() as session:
        db = await session.get_one(Resource, ids["db"])
    waits: list[str] = []
    checks = await k8s_checks(
        cluster(now, healthy=True),
        db,
        since=now - timedelta(minutes=3),
        now=now,
        check_logs=False,
        waits=waits,
    )
    assert waits == []
    assert {c.name: c.ok for c in checks} == {"rollout": True, "health": True}
    assert checks[0].detail == "db: 2/2 pods ready, revision 4"

    rolling = cluster(now, healthy=True)
    rolling.items["statefulsets"][0]["status"]["currentRevision"] = f"{NAME}-rev3"
    rolling.items["statefulsets"][0]["status"]["updatedReplicas"] = 1
    waits = []
    checks = await k8s_checks(
        rolling,
        db,
        since=now - timedelta(minutes=3),
        now=now,
        check_logs=False,
        waits=waits,
    )
    assert waits == ["db rollout in progress: 1/2 pods updated"]
    assert {c.name: c.ok for c in checks} == {"rollout": False, "health": True}


# --- Medic end to end -------------------------------------------------------------------------


def _evidence(db: uuid.UUID, now: datetime) -> tuple[Evidence, str, str]:
    ev = Evidence()
    key = f"k8s:statefulset/{NAME}"
    ev.sources[key] = [
        line(now - timedelta(minutes=12), "k8s", NAME, "revision 3 rolled out: image db:1.5"),
        line(now - timedelta(minutes=1), "k8s", NAME, "pod db-0 container db: CrashLoopBackOff"),
    ]
    ev.truncated[key] = False
    ev.source_resources[key] = db
    ev.changes = [
        ChangeMarker(
            kind="deploy",
            ref="shop/db revision 3",
            at=now - timedelta(minutes=12),
            label="db revision 3 (db:1.5)",
            resource_id=db,
        )
    ]
    return ev, eid(ev, key, "rolled out"), eid(ev, key, "CrashLoopBackOff")


async def test_medic_rolls_a_crashing_statefulset_back_and_resolves(
    session_factory: SessionFactory, workspace: Workspace, use_deps: Any
) -> None:
    sf = session_factory
    ids = await _graph(sf, workspace.id)
    now = _now()
    live = {"cluster": cluster(now)}
    ev, rollout_line, crash_line = _evidence(ids["db"], now)
    root = {
        "summary": "Revision 3 (db:1.5) crashes on start.",
        "root_cause": "The db:1.5 image fails its migration on start.",
        "claims": [
            {"text": "Revision 3 rolled out db:1.5.", "evidence_ids": [rollout_line]},
            {"text": "Its pods crash-loop.", "evidence_ids": [crash_line]},
        ],
        "triggering_change": {"kind": "deploy", "ref": "shop/db revision 3"},
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
            resource_ids=[ids["db"]],
            detected_by="watcher",
            signals=[
                Signal(
                    rule="k8s_pods_crashing",
                    resource_id=ids["db"],
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
    assert fix.fix.action == "k8s_rollout_undo" and fix.fix.title == "Roll back db to revision 2"
    assert fix.fix.plan is not None and fix.fix.plan.params["kind"] == "statefulset"
    assert [(p.what, p.before, p.after) for p in fix.fix.plan_lines] == [
        ("db pod template", "revision 3", "revision 2")
    ]

    await _decide(sf, workspace, fix.approval_id, Decision.approve)
    await resume_run_job(sf, {"run_id": str(run_id), "approval_id": str(fix.approval_id)})
    (patched,) = live["cluster"].patches
    assert patched[:2] == (NS, NAME)
    assert patched[2] == [{"op": "replace", "path": "/spec/template", "value": _template("db:1.4")}]
    sts = live["cluster"].items["statefulsets"][0]
    assert sts["spec"]["template"]["spec"]["containers"][0]["image"] == "db:1.4"
    incident, state, _ = await _state(sf, incident_id)
    assert incident.status == "verifying"

    live["cluster"] = cluster(now, healthy=True)
    payload = {"incident_id": str(incident_id), "run_id": str(run_id)}
    await verify_incident_job(sf, payload)
    incident, state, _ = await _state(sf, incident_id)
    assert state.verify is not None and state.verify.ok, state.verify
    await verify_incident_job(sf, payload)
    incident, state, _ = await _state(sf, incident_id)
    assert incident.status == "resolved"


# --- ConfigMaps and Secrets (metadata only) ------------------------------------------------------


def _config_cluster(now: datetime) -> FixtureKubeReader:
    """The shop/web Deployment, its template using ConfigMap web-config (envFrom) and Secret
    web-db (secretKeyRef). Both changed 5 minutes ago; ConfigMap other changed too but is not
    used. The fixture items hold values, as a real cluster would."""
    reader = deployment_cluster(now)
    container = reader.items["deployments"][0]["spec"]["template"]["spec"]["containers"][0]
    container["envFrom"] = [{"configMapRef": {"name": "web-config"}}]
    container["env"] = [
        {"name": "DB_URL", "valueFrom": {"secretKeyRef": {"name": "web-db", "key": "url"}}}
    ]

    def item(name: str, version: str, data: dict[str, str]) -> dict[str, Any]:
        return {
            "metadata": {
                "name": name,
                "namespace": NS,
                "resourceVersion": version,
                "creationTimestamp": _iso(now - timedelta(days=3)),
                "annotations": {"kubectl.kubernetes.io/last-applied-configuration": SECRET_VALUE},
                "managedFields": [
                    {
                        "manager": "kubectl",
                        "operation": "Update",
                        "time": _iso(now - timedelta(days=3)),
                        "fieldsV1": {"f:data": {"f:url": {}}},
                    },
                    {
                        "manager": "kubectl-edit",
                        "operation": "Update",
                        "time": _iso(now - timedelta(minutes=5)),
                    },
                ],
            },
            "data": data,
        }

    reader.items["configmaps"] = [
        item("web-config", "1234", {"PRICE_API": SECRET_VALUE}),
        item("other", "1300", {"X": "y"}),
    ]
    reader.items["secrets"] = [item("web-db", "99", {"url": SECRET_VALUE})]
    return reader


async def test_configmap_and_secret_changes_are_markers_from_metadata_only() -> None:
    now = _now()
    reader = _config_cluster(now)
    out = await k8s.read_workload(reader, NS, "web", now - timedelta(minutes=30), now)
    markers = {c.kind: c for c in out.changes if c.kind != "deploy"}
    assert set(markers) == {"config", "secret_change"}
    assert markers["config"].ref == "shop/configmap/web-config@1234"
    assert markers["secret_change"].ref == "shop/secret/web-db@99"
    assert markers["config"].at == now - timedelta(minutes=5)
    assert markers["config"].label == "ConfigMap shop/web-config changed"
    assert not any("other" in c.ref for c in out.changes)
    text = "\n".join(entry.message for entry in out.lines)
    assert "ConfigMap web-config (used by web) changed, resourceVersion 1234" in text
    assert "Secret web-db (used by web) changed, resourceVersion 99" in text
    assert SECRET_VALUE not in text and SECRET_VALUE not in str(out.changes)
    assert {"metadata configmaps", "metadata secrets"} <= set(reader.calls)
    assert "secrets" not in reader.calls and "configmaps" not in reader.calls
    # What the reader hands back has no value in it, whatever the cluster held.
    (secret,) = await reader.list_metadata("secrets", NS)
    assert set(secret) == {"metadata"} and SECRET_VALUE not in json.dumps(secret)
    with pytest.raises(KubeReadRefusedError):
        await reader.list("secrets", NS)
    with pytest.raises(KubeReadRefusedError):
        await reader.list_metadata("pods", NS)


async def test_without_secrets_rights_secret_changes_are_skipped_with_a_note() -> None:
    now = _now()

    class NoSecrets(FixtureKubeReader):
        async def list_metadata(self, kind: str, namespace: str) -> list[dict[str, Any]]:
            if kind == "secrets":
                raise ConnectorError("The kubeconfig's identity may not list secrets (403).")
            return await super().list_metadata(kind, namespace)

    base = _config_cluster(now)
    reader = NoSecrets(base.items, base.logs)
    out = await k8s.read_workload(reader, NS, "web", now - timedelta(minutes=30), now)
    assert {c.kind for c in out.changes} == {"deploy", "config"}
    text = "\n".join(entry.message for entry in out.lines)
    assert "Secret changes not checked: The kubeconfig's identity may not list secrets" in text


class _Response:
    def __init__(self, body: dict[str, Any]) -> None:
        self.data = json.dumps(body).encode()


class _FakeApiClient:
    """Stands in for kubernetes.client.ApiClient: asserts the metadata-only request."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.requests: list[dict[str, Any]] = []

    def call_api(self, path: str, method: str, **kw: Any) -> _Response:
        assert method == "GET"
        assert kw["header_params"] == {"Accept": METADATA_ACCEPT}
        assert kw["_preload_content"] is False
        self.requests.append({"path": path, **kw})
        return _Response(self.body)


def _live(api: _FakeApiClient) -> LiveKubeReader:
    reader = LiveKubeReader.__new__(LiveKubeReader)
    reader._api = api
    reader.server = "https://1.2.3.4"
    reader.max_items = 2000
    reader.calls = []
    return reader


async def test_the_live_reader_asks_for_partial_object_metadata() -> None:
    assert METADATA_ACCEPT == "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"
    api = _FakeApiClient(
        {
            "kind": "PartialObjectMetadataList",
            "apiVersion": "meta.k8s.io/v1",
            "metadata": {},
            "items": [
                {
                    "kind": "PartialObjectMetadata",
                    "metadata": {
                        "name": "web-db",
                        "namespace": NS,
                        "resourceVersion": "99",
                        "annotations": {"note": SECRET_VALUE},
                        "managedFields": [{"manager": "kubectl", "time": "2026-10-19T10:00:00Z"}],
                    },
                }
            ],
        }
    )
    reader = _live(api)
    (item,) = await reader.list_metadata("secrets", NS)
    (request,) = api.requests
    assert request["path"] == "/api/v1/namespaces/{namespace}/secrets"
    assert request["path_params"] == {"namespace": NS}
    assert item["metadata"]["resourceVersion"] == "99"
    assert SECRET_VALUE not in json.dumps(item)
    assert reader.calls == ["metadata secrets"]


async def test_the_live_reader_refuses_a_response_with_data() -> None:
    """A server that ignored the Accept header and sent a SecretList: refused, nothing kept."""
    api = _FakeApiClient(
        {
            "kind": "SecretList",
            "items": [{"metadata": {"name": "web-db"}, "data": {"url": SECRET_VALUE}}],
        }
    )
    with pytest.raises(ConnectorError, match="did not return secrets as metadata only") as exc:
        await _live(api).list_metadata("secrets", NS)
    assert SECRET_VALUE not in str(exc.value)
    assert "configmaps" in kube.K8S_METADATA_KINDS and "secrets" not in kube.K8S_READ_KINDS


async def test_a_configmap_change_gets_a_restore_note_not_a_rollback(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    sf = session_factory
    now = _now()
    ids = await _graph(sf, workspace.id)
    async with sf() as session:
        db = await session.get_one(Resource, ids["db"])
        conn = await session.get_one(Connection, ids["conn"])
    graph = fx.Graph({db.id: db}, [], {conn.id: conn}, [db.id])
    at = datetime(2026, 10, 19, 9, 55, tzinfo=now.tzinfo)
    changes = [
        ChangeMarker(
            kind="config",
            ref="shop/configmap/db-config@1234",
            at=at,
            label="ConfigMap shop/db-config changed",
            resource_id=db.id,
        ),
        ChangeMarker(
            kind="deploy",
            ref="shop/db revision 2",
            at=at - timedelta(days=2),
            label="db revision 2",
            resource_id=db.id,
        ),
    ]
    root = RootCause(
        summary="s",
        root_cause="r",
        triggering_change={"kind": "config", "ref": "shop/configmap/db-config@1234"},
        confidence="high",
        fix_options=[{"kind": "k8s_rollout_undo", "description": "-"}],
    )
    signals = [
        Signal(
            rule="k8s_pods_crashing",
            resource_id=db.id,
            resource_name=NAME,
            detail="2 pods crashing",
            at=now,
            down=True,
        )
    ]
    k8s_actions = K8sEmergencyActions(sf, settings(), operator_for=lambda c: cluster(now))
    out = await fx.emergency(
        root, signals, graph, changes, FakeEmergencyActions(sf, kube=k8s_actions)
    )
    assert out.records == []
    assert out.notes == [
        "ConfigMap shop/db-config changed at 2026-10-19 09:55 UTC; restore it, then Groundcrew "
        "can restart db. Groundcrew never reads its values, so it offers no rollback of its own."
    ]
    # A Kubernetes Secret named loosely ("secret_change" without a ref) goes the same way, and
    # never reaches the AWS secret rollback.
    secret = changes[0].model_copy(
        update={"kind": "secret_change", "ref": "shop/secret/db-pass@7", "label": "-"}
    )
    loose = RootCause(
        summary="s",
        root_cause="r",
        triggering_change={"kind": "secret_change"},
        confidence="high",
        fix_options=[{"kind": "secret_rollback_version", "description": "-"}],
    )
    out = await fx.emergency(
        loose, signals, graph, [secret], FakeEmergencyActions(sf, kube=k8s_actions)
    )
    assert out.records == [] and out.notes[0].startswith("Secret shop/db-pass changed at")
