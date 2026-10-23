"""agent_events, agent_commands, and the agents' live service states (signals and fixes).

Revision ID: c3e8f1a2b4d6
Revises: 7a1c4e2b9d30
Create Date: 2026-10-22 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3e8f1a2b4d6"
down_revision: str | None = "7a1c4e2b9d30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents", sa.Column("states", postgresql.JSONB(), server_default="{}", nullable=False)
    )
    op.add_column("agents", sa.Column("states_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "agents", sa.Column("firing", postgresql.JSONB(), server_default="{}", nullable=False)
    )
    op.create_table(
        "agent_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("service", sa.String(100), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], ondelete="CASCADE", name="fk_agent_events_agent_id_agents"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_events"),
    )
    op.create_index(
        "ix_agent_events_agent_service_at", "agent_events", ["agent_id", "service", "at"]
    )
    op.create_index("ix_agent_events_at", "agent_events", ["at"])
    op.create_table(
        "agent_commands",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("service", sa.String(100), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("signature", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), server_default="queued", nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            ondelete="CASCADE",
            name="fk_agent_commands_agent_id_agents",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name="fk_agent_commands_workspace_id_workspaces",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], ondelete="SET NULL", name="fk_agent_commands_run_id_runs"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_commands"),
    )
    op.create_index("ix_agent_commands_agent_status", "agent_commands", ["agent_id", "status"])
    op.create_index("ix_agent_commands_workspace_id", "agent_commands", ["workspace_id"])


def downgrade() -> None:
    op.drop_table("agent_commands")
    op.drop_table("agent_events")
    op.drop_column("agents", "firing")
    op.drop_column("agents", "states_at")
    op.drop_column("agents", "states")
