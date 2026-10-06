"""Seed a dev user and a "Demo" workspace. Safe to run more than once.

Real users get their own workspace on first GitHub sign-in; this seed is only for scripts and
API experiments without the web app.

Run: make seed  (uses the local compose database unless DB_URL is passed to make)
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.db.models import User, Workspace, WorkspaceMember, WorkspaceRole
from app.db.session import get_engine, get_session_factory

DEV_EMAIL = "dev@groundcrew.local"
WORKSPACE_NAME = "Demo"


async def seed() -> None:
    async with get_session_factory()() as session:
        user = await session.scalar(select(User).where(User.email == DEV_EMAIL))
        if user is None:
            user = User(email=DEV_EMAIL, name="Dev User")
            session.add(user)
            await session.flush()

        workspace = await session.scalar(
            select(Workspace).where(Workspace.owner_id == user.id, Workspace.name == WORKSPACE_NAME)
        )
        if workspace is None:
            workspace = Workspace(name=WORKSPACE_NAME, owner_id=user.id)
            session.add(workspace)
            await session.flush()
        member = await session.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace.id, WorkspaceMember.user_id == user.id
            )
        )
        if member is None:
            session.add(
                WorkspaceMember(
                    workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.owner
                )
            )

        await session.commit()
        print(f"user      {user.id}  {user.email}")
        print(f"workspace {workspace.id}  {workspace.name}")
    await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(seed())
