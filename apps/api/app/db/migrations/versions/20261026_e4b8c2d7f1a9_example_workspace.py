"""workspaces.is_example: the read-only Example workspace every signed-in user can open.

Revision ID: e4b8c2d7f1a9
Revises: a7c3e9f1b2d4
Create Date: 2026-10-26 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4b8c2d7f1a9"
down_revision: str | None = "a7c3e9f1b2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("is_example", sa.Boolean(), server_default="false", nullable=False),
    )
    # At most one example workspace.
    op.create_index(
        "uq_workspaces_example",
        "workspaces",
        ["is_example"],
        unique=True,
        postgresql_where=sa.text("is_example"),
    )


def downgrade() -> None:
    op.drop_index("uq_workspaces_example", table_name="workspaces")
    op.drop_column("workspaces", "is_example")
