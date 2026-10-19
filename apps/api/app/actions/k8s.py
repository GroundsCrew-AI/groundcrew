"""Emergency actions on a Kubernetes Deployment or StatefulSet, through the connection's
kubeconfig.

Only for connections with the operate capability: an owner turns it on (POST
/connections/{id}/k8s/operate), and Groundcrew first checks with SelfSubjectAccessReviews that
the kubeconfig's identity may patch Deployments and list ReplicaSets, or patch StatefulSets and
list ControllerRevisions (infra/k8s-operate-rbac.yaml grants exactly that). Every action still
needs a person's approval; `execute` consumes it before the write and re-reads the workload
first: if its revision moved since the plan, nothing is written (ok=False).

- k8s_rollout_undo: what `kubectl rollout undo` does. The pod template of the previous
  revision (a Deployment's ReplicaSet without its pod-template-hash label, a StatefulSet's
  ControllerRevision `data.spec.template`) replaces the workload's template; Kubernetes rolls
  it out as a new revision.
- k8s_rollout_restart: what `kubectl rollout restart` does. The template gets a
  kubectl.kubernetes.io/restartedAt annotation, so every pod is replaced (they read
  ConfigMaps and Secrets again).

Plans carry `params` {"namespace", "deployment": <name>} for a Deployment (as before, so its
plan hashes do not change) and {"namespace", "statefulset": <name>, "kind": "statefulset"} for
a StatefulSet (`app.actions.base.k8s_workload` reads either). `current` and `desired` are
"<namespace>/<name> revision <n>", the same text as the deploy change markers
(app.telemetry.k8s.change_ref), so Medic can tell a direct revert.
"""

import copy
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.actions.base import (
    K8S_ROLLOUT_RESTART,
    K8S_ROLLOUT_UNDO,
    ActionPlan,
    ActionResult,
    EmergencyActionError,
    k8s_params,
    k8s_workload,
)
from app.config import Settings, get_settings
from app.connectors.credentials import capabilities
from app.connectors.kubernetes import KubeOperator
from app.db.models import Connection
from app.db.session import SessionFactory
from app.discovery.types import ConnectorError
from app.policy.actions import ApprovedAction, consume_approved_action
from app.telemetry import k8s

RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"
OperatorFor = Callable[[Connection], KubeOperator]
# What the kubeconfig's identity must be allowed to do, per workload kind: (verb, resource).
OPERATE_RIGHTS: dict[str, tuple[tuple[str, str], ...]] = {
    k8s.DEPLOYMENT: (("patch", "deployments"),),
    k8s.STATEFULSET: (("patch", "statefulsets"), ("list", "controllerrevisions")),
}


def default_operator_for(connection: Connection) -> KubeOperator:
    from app.connectors import kubernetes
    from app.security.crypto import decrypt

    if not connection.secret_encrypted:
        raise EmergencyActionError(f"{connection.name} has no kubeconfig.")
    return kubernetes.live_operator(decrypt(connection.secret_encrypted))


def _close(operator: KubeOperator) -> None:
    close = getattr(operator, "close", None)
    if close is not None:
        close()


def rollback_template(revision: dict[str, Any]) -> dict[str, Any]:
    """The revision's pod template as the workload's template. A ReplicaSet's
    pod-template-hash label is Kubernetes' own and is dropped (kubectl rollout undo does the
    same); a ControllerRevision's stored "$patch" marker is dropped too."""
    template = copy.deepcopy(k8s.revision_template(revision))
    labels = (template.get("metadata") or {}).get("labels")
    if isinstance(labels, dict):
        labels.pop(k8s.POD_TEMPLATE_HASH, None)
    return template


async def _patch(
    operator: KubeOperator, kind: str, namespace: str, name: str, patch: list[dict[str, Any]]
) -> None:
    if kind == k8s.STATEFULSET:
        await operator.patch_statefulset(namespace, name, patch)
    else:
        await operator.patch_deployment(namespace, name, patch)


class K8sEmergencyActions:
    def __init__(
        self,
        session_factory: SessionFactory,
        settings: Settings | None = None,
        *,
        operator_for: OperatorFor | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self._operator_for = operator_for or default_operator_for

    async def _operator(self, connection_id: uuid.UUID) -> KubeOperator:
        async with self.session_factory() as session:
            conn = await session.scalar(select(Connection).where(Connection.id == connection_id))
        if conn is None or conn.kind != "k8s":
            raise EmergencyActionError("That Kubernetes connection does not exist.")
        if conn.status != "connected" or "operate" not in capabilities(conn):
            raise EmergencyActionError(
                f"Emergency fixes are off for {conn.name}: turn them on for the cluster on the "
                "Connections page (its kubeconfig needs infra/k8s-operate-rbac.yaml)."
            )
        try:
            return self._operator_for(conn)
        except ConnectorError as exc:
            raise EmergencyActionError(str(exc)) from None

    async def _workload(
        self, operator: KubeOperator, namespace: str, name: str, kind: str = k8s.DEPLOYMENT
    ) -> k8s.Workload:
        """The workload as it is now. Refuses first when the kubeconfig's identity may not
        patch it (and, for a StatefulSet, list its ControllerRevisions) in its namespace, so
        nobody approves a fix the cluster would refuse."""
        label = k8s.KINDS[k8s.check_workload_kind(kind)][2]
        try:
            for verb, resource in OPERATE_RIGHTS[kind]:
                if not await operator.can_i(verb, "apps", resource, namespace):
                    what = f"{label}s" if verb == "patch" else "ControllerRevisions"
                    raise EmergencyActionError(
                        f"The cluster's kubeconfig may not {verb} {what} in {namespace} "
                        "(infra/k8s-operate-rbac.yaml grants it)."
                    )
            found = await k8s.fetch_workload(operator, namespace, name, kind)
        except ConnectorError as exc:
            raise EmergencyActionError(str(exc)) from None
        if found is None:
            raise EmergencyActionError(f"{label} {namespace}/{name} not found.")
        return found

    @staticmethod
    def _previous(workload: k8s.Workload) -> tuple[int, dict[str, Any]]:
        current = k8s.current_revision(workload)
        older = [
            rs
            for rs in workload.revisions
            if (k8s.revision(rs) or 0) < (current or 0) and k8s.revision_template(rs)
        ]
        if current is None or not older:
            raise EmergencyActionError(f"{workload.name} has no previous revision to roll back to.")
        previous = older[-1]
        return k8s.revision(previous) or 0, previous

    # ------------------------------------------------------------------ plans (read)

    async def plan_k8s_rollout_undo(
        self,
        connection_id: uuid.UUID,
        namespace: str,
        name: str,
        kind: str = k8s.DEPLOYMENT,
    ) -> ActionPlan:
        operator = await self._operator(connection_id)
        try:
            workload = await self._workload(operator, namespace, name, kind)
        finally:
            _close(operator)
        current = k8s.current_revision(workload)
        previous, rs = self._previous(workload)
        now_image = k8s.images(workload.template)
        old_image = k8s.images(k8s.revision_template(rs))
        return ActionPlan(
            action=K8S_ROLLOUT_UNDO,
            connection_id=connection_id,
            target=f"{namespace}/{name}",
            params=k8s_params(namespace, name, kind),
            current=k8s.change_ref(namespace, name, current),
            desired=k8s.change_ref(namespace, name, previous),
            summary=(
                f"Roll {name} back to revision {previous} ({old_image}); now revision "
                f"{current} ({now_image})"
            ),
            planned_at=datetime.now(UTC),
        )

    async def plan_k8s_rollout_restart(
        self,
        connection_id: uuid.UUID,
        namespace: str,
        name: str,
        kind: str = k8s.DEPLOYMENT,
    ) -> ActionPlan:
        operator = await self._operator(connection_id)
        try:
            workload = await self._workload(operator, namespace, name, kind)
        finally:
            _close(operator)
        current = k8s.current_revision(workload)
        return ActionPlan(
            action=K8S_ROLLOUT_RESTART,
            connection_id=connection_id,
            target=f"{namespace}/{name}",
            params=k8s_params(namespace, name, kind),
            current=k8s.change_ref(namespace, name, current),
            desired=None,
            summary=f"Replace every {name} pod (revision {current}, same template)",
            planned_at=datetime.now(UTC),
        )

    # ------------------------------------------------------------------ execute (write)

    async def execute(
        self, plan: ActionPlan, token: ApprovedAction, *, run_id: uuid.UUID
    ) -> ActionResult:
        started = datetime.now(UTC)
        await consume_approved_action(
            self.session_factory,
            token,
            run_id=run_id,
            action=plan.action,
            plan_hash=plan.plan_hash(),
            settings=self.settings,
        )

        def result(ok: bool, summary: str, **kw: Any) -> ActionResult:
            return ActionResult(
                action=plan.action,
                ok=ok,
                summary=summary,
                started_at=started,
                finished_at=datetime.now(UTC),
                **kw,
            )

        kind, namespace, name = k8s_workload(plan.params)
        try:
            operator = await self._operator(plan.connection_id)
        except EmergencyActionError as exc:
            return result(False, "Not run.", error=str(exc))
        try:
            workload = await self._workload(operator, namespace, name, kind)
            now = k8s.change_ref(namespace, name, k8s.current_revision(workload))
            if now != plan.current:
                return result(
                    False,
                    f"Not run: the {workload.label} changed since the plan was approved.",
                    error=f"it is at {now}, the plan expected {plan.current}",
                )
            if plan.action == K8S_ROLLOUT_UNDO:
                wanted = next(
                    (
                        rs
                        for rs in workload.revisions
                        if k8s.change_ref(namespace, name, k8s.revision(rs)) == plan.desired
                    ),
                    None,
                )
                if wanted is None:
                    return result(
                        False,
                        "Not run: the previous revision is gone.",
                        error=(
                            "no "
                            + ("ControllerRevision" if kind == k8s.STATEFULSET else "ReplicaSet")
                            + f" for {plan.desired}"
                        ),
                    )
                template = rollback_template(wanted)
                await _patch(
                    operator,
                    kind,
                    namespace,
                    name,
                    [{"op": "replace", "path": "/spec/template", "value": template}],
                )
                return result(
                    True,
                    plan.summary,
                    details={"image": k8s.images(template), "rolled_back_to": str(plan.desired)},
                )
            template_meta = workload.template.get("metadata") or {}
            annotations = {
                **(template_meta.get("annotations") or {}),
                RESTARTED_AT: datetime.now(UTC).isoformat(),
            }
            await _patch(
                operator,
                kind,
                namespace,
                name,
                [
                    {
                        # add: creates the annotations map when the template has none.
                        "op": "add",
                        "path": "/spec/template/metadata/annotations",
                        "value": annotations,
                    }
                ],
            )
            return result(True, plan.summary, details={"restarted_at": annotations[RESTARTED_AT]})
        except (EmergencyActionError, ConnectorError) as exc:
            return result(False, "The cluster refused the action.", error=str(exc))
        finally:
            _close(operator)
