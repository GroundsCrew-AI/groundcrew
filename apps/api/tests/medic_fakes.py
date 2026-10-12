"""Helpers for Medic's tests: approvals for Medic actions, and the checkout-demo shapes."""

import uuid

from app.actions.fake import FakeSecret, FakeService
from app.db.models import Approval, ApprovalStatus, Run, RunStatus, ToolPolicy, Workspace
from app.db.session import SessionFactory
from app.policy.actions import ApprovedAction, MedicAction, issue_approved_action

ACCOUNT = "881050425883"
TASK_DEF = f"arn:aws:ecs:us-east-2:{ACCOUNT}:task-definition"
SECRET_ARN = f"arn:aws:secretsmanager:us-east-2:{ACCOUNT}:secret:checkout/database-url-AbC123"


def checkout_services() -> dict[str, FakeService]:
    return {
        "checkout/checkout-worker": FakeService(
            revisions=[f"{TASK_DEF}/checkout-worker:41", f"{TASK_DEF}/checkout-worker:42"]
        ),
        "checkout/checkout-api": FakeService(revisions=[f"{TASK_DEF}/checkout-api:17"]),
    }


def checkout_secrets() -> dict[str, FakeSecret]:
    return {SECRET_ARN: FakeSecret(versions={"v-good": ["AWSPREVIOUS"], "v-bad": ["AWSCURRENT"]})}


async def medic_run(sf: SessionFactory, workspace: Workspace) -> Run:
    async with sf() as session:
        run = Run(workspace_id=workspace.id, agent="medic", goal="g", status=RunStatus.running)
        session.add(run)
        await session.commit()
        return run


async def approve_medic_action(
    sf: SessionFactory,
    workspace: Workspace,
    run: Run,
    *,
    action: MedicAction,
    plan_hash: str,
) -> ApprovedAction:
    """An approved approval for `action` on `plan_hash` in `run`, and its token."""
    async with sf() as session:
        approval = Approval(
            run_id=run.id,
            workspace_id=workspace.id,
            tool_call_id=f"medic-{uuid.uuid4()}",
            tool_name=action,
            policy=ToolPolicy.write if action == "open_fix_pr" else ToolPolicy.emergency,
            arguments={"plan_hash": plan_hash},
            status=ApprovalStatus.approved,
        )
        session.add(approval)
        await session.commit()
        return await issue_approved_action(
            session, approval, run_id=run.id, action=action, plan_hash=plan_hash
        )
