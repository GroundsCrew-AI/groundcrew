"""incidents: detection columns (Phase 5 M2).

Who opened it and which rule fired first, the investigation window, affected resources and the
resource group used for dedupe, signals, and Medic's state for the API.

Revision ID: 9c6e4a1f3d52
Revises: 8b5d3f0e2c41
Create Date: 2026-10-13 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9c6e4a1f3d52"
down_revision: str | None = "8b5d3f0e2c41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuids = postgresql.ARRAY(sa.Uuid())
    op.add_column(
        "incidents", sa.Column("detected_by", sa.String(16), server_default="user", nullable=False)
    )
    op.add_column(
        "incidents",
        sa.Column("detector_rule", sa.String(32), server_default="user_report", nullable=False),
    )
    op.add_column(
        "incidents",
        sa.Column(
            "window_start", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column("incidents", sa.Column("window_end", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "incidents", sa.Column("first_signal_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "incidents",
        sa.Column("affected_resource_ids", uuids, server_default="{}", nullable=False),
    )
    op.add_column(
        "incidents", sa.Column("group_resource_ids", uuids, server_default="{}", nullable=False)
    )
    op.add_column(
        "incidents",
        sa.Column("signals", postgresql.JSONB(), server_default="[]", nullable=False),
    )
    op.add_column(
        "incidents", sa.Column("state", postgresql.JSONB(), server_default="{}", nullable=False)
    )
    op.create_index("ix_incidents_workspace_status", "incidents", ["workspace_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_incidents_workspace_status", table_name="incidents")
    for column in (
        "state",
        "signals",
        "group_resource_ids",
        "affected_resource_ids",
        "first_signal_at",
        "window_end",
        "window_start",
        "detector_rule",
        "detected_by",
    ):
        op.drop_column("incidents", column)
