"""platform_settings: run-time switches for the whole platform (signups_paused).

Revision ID: 11cd808b2d33
Revises: dc23308a43f8
Create Date: 2026-10-21 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "11cd808b2d33"
down_revision: str | None = "dc23308a43f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_settings",
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            ondelete="SET NULL",
            name="fk_platform_settings_updated_by_users",
        ),
        sa.PrimaryKeyConstraint("key", name="pk_platform_settings"),
    )


def downgrade() -> None:
    op.drop_table("platform_settings")
