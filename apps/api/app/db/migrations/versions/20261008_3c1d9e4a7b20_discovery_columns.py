"""Discovery: resource account, stale flag and last seen time; edge confidence; connection's
last discovery.

Revision ID: 3c1d9e4a7b20
Revises: 7a6b5d2dc055
Create Date: 2026-10-08 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3c1d9e4a7b20"
down_revision: str | None = "7a6b5d2dc055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("resources", sa.Column("account", sa.String(length=200), nullable=True))
    op.add_column(
        "resources", sa.Column("stale", sa.Boolean(), server_default="false", nullable=False)
    )
    op.add_column("resources", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "edges",
        sa.Column("confidence", sa.String(length=8), server_default="medium", nullable=False),
    )
    op.add_column(
        "connections",
        sa.Column(
            "last_discovery",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("connections", "last_discovery")
    op.drop_column("edges", "confidence")
    op.drop_column("resources", "last_seen_at")
    op.drop_column("resources", "stale")
    op.drop_column("resources", "account")
