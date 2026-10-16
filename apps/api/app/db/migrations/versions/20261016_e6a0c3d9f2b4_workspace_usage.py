"""llm_workspace_usage: model spend per workspace and UTC day (per-workspace caps).

Revision ID: e6a0c3d9f2b4
Revises: d5f9b2c8e1a3
Create Date: 2026-10-16 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6a0c3d9f2b4"
down_revision: str | None = "d5f9b2c8e1a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_workspace_usage",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("usd", sa.Numeric(12, 6), server_default="0", nullable=False),
        sa.Column("tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name="fk_llm_workspace_usage_workspace_id_workspaces",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "day", name="pk_llm_workspace_usage"),
    )


def downgrade() -> None:
    op.drop_table("llm_workspace_usage")
