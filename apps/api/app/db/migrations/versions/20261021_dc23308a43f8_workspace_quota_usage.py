"""workspace_quota_usage: daily Tavily calls and builds per workspace (app.quotas).

Revision ID: dc23308a43f8
Revises: 15c24cbee0a7
Create Date: 2026-10-21 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "dc23308a43f8"
down_revision: str | None = "15c24cbee0a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_quota_usage",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("count", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name="fk_workspace_quota_usage_workspace_id_workspaces",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "day", "kind", name="pk_workspace_quota_usage"),
    )


def downgrade() -> None:
    op.drop_table("workspace_quota_usage")
