"""agents and agent_nonces: Groundcrew agents on hosts (connection kind `agent`).

Revision ID: 7a1c4e2b9d30
Revises: b49ed03d6ca5
Create Date: 2026-10-22 13:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a1c4e2b9d30"
down_revision: str | None = "b49ed03d6ca5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("join_token_hash", sa.String(64), nullable=True),
        sa.Column("join_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("public_key", sa.String(64), nullable=True),
        sa.Column("hostname", sa.String(253), nullable=True),
        sa.Column("os", sa.String(64), nullable=True),
        sa.Column("arch", sa.String(32), nullable=True),
        sa.Column("version", sa.String(32), nullable=True),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
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
            name="fk_agents_workspace_id_workspaces",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            ondelete="CASCADE",
            name="fk_agents_connection_id_connections",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agents"),
        sa.UniqueConstraint("connection_id", name="uq_agents_connection_id"),
        sa.UniqueConstraint("join_token_hash", name="uq_agents_join_token_hash"),
    )
    op.create_index("ix_agents_workspace_id", "agents", ["workspace_id"])
    op.create_table(
        "agent_nonces",
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("nonce", sa.String(64), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            ondelete="CASCADE",
            name="fk_agent_nonces_agent_id_agents",
        ),
        sa.PrimaryKeyConstraint("agent_id", "nonce", name="pk_agent_nonces"),
    )
    op.create_index("ix_agent_nonces_seen_at", "agent_nonces", ["seen_at"])


def downgrade() -> None:
    op.drop_table("agent_nonces")
    op.drop_table("agents")
