"""workspace_invites: GitHub logins invited to a workspace (members API).

Revision ID: d5f9b2c8e1a3
Revises: c3e8a1f4b6d7
Create Date: 2026-10-16 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d5f9b2c8e1a3"
down_revision: str | None = "c3e8a1f4b6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    role = postgresql.ENUM("owner", "member", name="workspace_role", create_type=False)
    op.create_table(
        "workspace_invites",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("github_login", sa.String(100), nullable=False),
        sa.Column("role", role, nullable=False),
        sa.Column("invited_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name="fk_workspace_invites_workspace_id_workspaces",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by"],
            ["users.id"],
            ondelete="SET NULL",
            name="fk_workspace_invites_invited_by_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspace_invites"),
        sa.UniqueConstraint("workspace_id", "github_login", name="uq_workspace_invites_login"),
    )
    op.create_index("ix_workspace_invites_workspace_id", "workspace_invites", ["workspace_id"])
    op.create_index("ix_workspace_invites_github_login", "workspace_invites", ["github_login"])


def downgrade() -> None:
    op.drop_table("workspace_invites")
