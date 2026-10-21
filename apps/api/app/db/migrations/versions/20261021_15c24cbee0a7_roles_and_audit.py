"""Workspace roles owner / approver / viewer (member becomes approver), and audit_events.

Revision ID: 15c24cbee0a7
Revises: f7b1d4e0a3c5
Create Date: 2026-10-21 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "15c24cbee0a7"
down_revision: str | None = "f7b1d4e0a3c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("workspace_members", "workspace_invites")


def _retype(values: tuple[str, ...], mapping: dict[str, str]) -> None:
    """Replace the workspace_role type with one holding `values`, mapping old values."""
    op.execute("ALTER TYPE workspace_role RENAME TO workspace_role_old")
    op.execute("CREATE TYPE workspace_role AS ENUM (" + ", ".join(f"'{v}'" for v in values) + ")")
    cases = " ".join(f"WHEN '{old}' THEN '{new}'" for old, new in mapping.items())
    for table in _TABLES:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN role TYPE workspace_role "
            f"USING (CASE role::text {cases} ELSE role::text END)::workspace_role"
        )
    op.execute("DROP TYPE workspace_role_old")


def upgrade() -> None:
    # Every member could approve before; they keep that as approvers.
    _retype(("owner", "approver", "viewer"), {"member": "approver"})
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("actor_label", sa.String(320), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=True),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=False),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name="fk_audit_events_workspace_id_workspaces",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["users.id"], ondelete="SET NULL", name="fk_audit_events_actor_id_users"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
    )
    op.create_index(
        "ix_audit_events_workspace_created", "audit_events", ["workspace_id", "created_at"]
    )
    op.create_index("ix_audit_events_actor_id", "audit_events", ["actor_id"])


def downgrade() -> None:
    op.drop_table("audit_events")
    _retype(("owner", "member"), {"approver": "member", "viewer": "member"})
