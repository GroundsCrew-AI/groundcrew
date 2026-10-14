"""Retry a failed emergency fix (POST /incidents/{id}/fixes/{fix_id}/retry).

An emergency fix that AWS refused (for example AccessDenied on iam:PassRole before the
connection's stack had OperatePassRoleArns) can be tried again once the permissions are fixed.
A retry never reuses the old approval: the fix is planned again from the current state, and a
new approval is requested on the same Medic run, which goes back to waiting_approval. Fixes
that were skipped only because this one failed (`depends_on`) are planned and requested again
too. Approving resumes the run like any other Medic approval.
"""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions.base import EmergencyActions, plan_again
from app.agents.medic.events import FixApproval
from app.agents.medic.fixes import plan_lines
from app.agents.medic.state import FixRecord, MedicState
from app.db.models import (
    Approval,
    ApprovalStatus,
    Incident,
    Run,
    RunStatus,
    StopReason,
    ToolPolicy,
)
from app.incidents.store import set_status
from app.steps import record_step

# Medic is between steps: a retry may reopen the run. While it investigates, waits for another
# approval, or applies, the retry waits.
RETRY_PHASES = ("done", "verifying")


class RetryError(Exception):
    """The fix cannot be retried now; the message says why and is safe to show."""


def _skipped_by(rec: FixRecord, fix_id: str) -> bool:
    error = rec.applied.error if rec.applied else None
    return (
        fix_id in rec.fix.depends_on
        and rec.status == "rejected"
        and bool(error and error.startswith("Not run: fix"))
    )


async def retry_fix(
    session: AsyncSession,
    incident: Incident,
    fix_id: str,
    actions: EmergencyActions,
    *,
    now: datetime,
) -> list[FixRecord]:
    """Plan the failed fix (and the fixes it held back) again and request new approvals. Returns
    the reopened records. Raises RetryError, or EmergencyActionError when the plan itself
    cannot be made (the message says what to change, e.g. "rollback needs PassRole on ...").
    Commits."""
    state = MedicState.model_validate(incident.state or {})
    rec = next((r for r in state.fixes if r.fix.fix_id == fix_id), None)
    if rec is None:
        raise RetryError("Fix not found on this incident.")
    if rec.fix.kind != "emergency" or rec.fix.plan is None or rec.status != "failed":
        raise RetryError("Only a failed emergency fix can be retried.")
    if incident.status == "resolved":
        raise RetryError("The incident is resolved.")
    if state.phase not in RETRY_PHASES or state.run_id is None:
        raise RetryError("Medic is still working on this incident; retry when it is done.")
    reopen = [rec] + [r for r in state.fixes if _skipped_by(r, fix_id)]
    run_id = state.run_id
    await session.commit()  # no transaction open during the AWS reads

    plans = {r.fix.fix_id: await plan_again(actions, r.fix.plan) for r in reopen if r.fix.plan}

    run = await session.get(Run, run_id, with_for_update=True, populate_existing=True)
    await session.refresh(incident, with_for_update=True)
    state = MedicState.model_validate(incident.state or {})
    if run is None or state.phase not in RETRY_PHASES:
        raise RetryError("Medic is still working on this incident; retry when it is done.")
    attempt = 1 + int(
        await session.scalar(
            select(func.count(Approval.id)).where(
                Approval.run_id == run.id,
                Approval.tool_call_id.like(f"medic:{incident.id}:{fix_id}:retry-%"),
            )
        )
        or 0
    )
    reopened: list[FixRecord] = []
    for i, r in enumerate(state.fixes):
        plan = plans.get(r.fix.fix_id)
        current = r.status == "failed" if r.fix.fix_id == fix_id else _skipped_by(r, fix_id)
        if plan is None or not current:
            continue
        approval = Approval(
            run_id=run.id,
            workspace_id=incident.workspace_id,
            tool_call_id=f"medic:{incident.id}:{r.fix.fix_id}:retry-{attempt}",
            tool_name=plan.action,
            policy=ToolPolicy.emergency,
            arguments={
                "plan_hash": plan.plan_hash(),
                "fix_id": r.fix.fix_id,
                "summary": r.fix.title,
                "retry": attempt,
            },
            status=ApprovalStatus.pending,
        )
        session.add(approval)
        await session.flush()
        fix = r.fix.model_copy(
            update={"plan": plan, "plan_lines": plan_lines(plan), "approval_id": approval.id}
        )
        state.fixes[i] = r.model_copy(
            update={
                "fix": fix,
                "status": "waiting_approval",
                "approval_id": approval.id,
                "applied": None,
            }
        )
        reopened.append(state.fixes[i])
        await record_step(
            session,
            run,
            kind="approval_requested",
            tool_name=plan.action,
            approval_id=approval.id,
            output_summary=f"Retry {attempt}: waiting for approval of {fix.title}",
            data=FixApproval(incident_id=incident.id, fix=fix),
        )
    state.phase, state.error, state.outcome = "waiting_approval", None, None
    state.verify_chain = uuid.uuid4().hex  # a verify job already queued finds a stale chain
    incident.state = state.model_dump(mode="json")
    await set_status(
        session,
        incident,
        run,
        "waiting_approval",
        reason=f"Retrying fix {rec.fix.order} after it failed",
        now=now,
    )
    run.status, run.stop_reason = RunStatus.waiting_approval, StopReason.waiting_approval
    run.result = run.error = None
    run.finished_at = None
    await session.commit()
    return reopened
