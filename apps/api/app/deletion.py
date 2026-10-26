"""Deleting a connection, a workspace, or an account, and everything Groundcrew stored for it.

- A connection: its stored credential (on the row), every resource it discovered with their
  edges (cascade), and the telemetry events read for those resources. Incidents stay as history
  (they name resources by id only).
- A workspace: everything in it, by cascade from `workspaces` (members, invites, connections,
  resources, runs and their steps and approvals, incidents, events, usage, audit log), plus the
  queued jobs of its runs. Cached cloud sessions for its connections are dropped at once.
- An account: the person's sign-in identities and links, their memberships, and every workspace
  where they are the only member. Shared workspaces stay: someone else must be an owner first.
  Their name stays on past approvals and in audit logs (actor_label), as a record of who did it.

What lives on the customer's side (the CloudFormation stack, the GitHub App install, the
kubeconfig's ServiceAccount) is theirs to remove; `cleanup_steps` says how.
"""

from dataclasses import dataclass, field

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.aws import get_sessions
from app.db.models import (
    Connection,
    Event,
    Job,
    JobStatus,
    Resource,
    Run,
    User,
    Workspace,
    WorkspaceMember,
    WorkspaceRole,
)


def forget_sessions(conn: Connection) -> None:
    """Drop cached AWS sessions for this connection's roles (no AWS call)."""
    c = conn.config or {}
    external_id = str(c.get("external_id") or "")
    if conn.kind != "aws" or not external_id:
        return
    for key in ("role_arn", "deploy_role_arn", "operate_role_arn"):
        if c.get(key):
            get_sessions().forget(str(c[key]), external_id)


async def delete_connection(session: AsyncSession, conn: Connection) -> int:
    """Delete the connection and its data; returns how many resources went. The caller
    commits."""
    resources = select(Resource.id).where(Resource.connection_id == conn.id)
    await session.execute(delete(Event).where(Event.resource_id.in_(resources)))
    result = await session.execute(delete(Resource).where(Resource.connection_id == conn.id))
    forget_sessions(conn)
    await session.delete(conn)
    return int(getattr(result, "rowcount", 0) or 0)


async def delete_workspace(session: AsyncSession, workspace: Workspace) -> None:
    """Delete the workspace and everything in it. The caller commits."""
    for conn in await session.scalars(
        select(Connection).where(Connection.workspace_id == workspace.id)
    ):
        forget_sessions(conn)
    run_ids = [
        str(r)
        for r in await session.scalars(select(Run.id).where(Run.workspace_id == workspace.id))
    ]
    if run_ids:
        # A queued job of a deleted run would only fail later; a running one stops at its next
        # step when the run is gone.
        await session.execute(
            delete(Job).where(
                Job.status == JobStatus.queued, Job.payload["run_id"].astext.in_(run_ids)
            )
        )
    await session.delete(workspace)


@dataclass
class AccountPlan:
    """What deleting an account does: workspaces deleted with it, and shared ones it blocks."""

    delete: list[Workspace] = field(default_factory=list)
    # Shared workspaces where this person is the only owner.
    blocked: list[Workspace] = field(default_factory=list)
    # Shared workspaces they just leave.
    leave: list[Workspace] = field(default_factory=list)


async def account_plan(session: AsyncSession, user: User) -> AccountPlan:
    plan = AccountPlan()
    rows = await session.execute(
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user.id)
        .order_by(Workspace.created_at)
    )
    for workspace, role in rows:
        members = int(
            await session.scalar(
                select(func.count(WorkspaceMember.id)).where(
                    WorkspaceMember.workspace_id == workspace.id
                )
            )
            or 0
        )
        if members <= 1:
            plan.delete.append(workspace)
            continue
        other_owners = int(
            await session.scalar(
                select(func.count(WorkspaceMember.id)).where(
                    WorkspaceMember.workspace_id == workspace.id,
                    WorkspaceMember.role == WorkspaceRole.owner,
                    WorkspaceMember.user_id != user.id,
                )
            )
            or 0
        )
        if role == WorkspaceRole.owner and other_owners == 0:
            plan.blocked.append(workspace)
        else:
            plan.leave.append(workspace)
    return plan


async def delete_account(session: AsyncSession, user: User, plan: AccountPlan) -> None:
    """Delete the account as planned (the caller checked `plan.blocked` is empty). Commits
    nothing."""
    for workspace in plan.delete:
        await delete_workspace(session, workspace)
    # The example workspace names whose workspace it was copied from as its owner (it has no
    # members); it goes with them and can be built again from another workspace.
    for workspace in await session.scalars(
        select(Workspace).where(Workspace.owner_id == user.id, Workspace.is_example.is_(True))
    ):
        await delete_workspace(session, workspace)
    await session.flush()
    for workspace in plan.leave:
        if workspace.owner_id == user.id:
            # workspaces.owner_id names its creator; hand it to an owner who stays.
            successor = await session.scalar(
                select(WorkspaceMember.user_id)
                .where(
                    WorkspaceMember.workspace_id == workspace.id,
                    WorkspaceMember.role == WorkspaceRole.owner,
                    WorkspaceMember.user_id != user.id,
                )
                .order_by(WorkspaceMember.created_at)
                .limit(1)
            )
            await session.execute(
                update(Workspace).where(Workspace.id == workspace.id).values(owner_id=successor)
            )
    await session.delete(user)
