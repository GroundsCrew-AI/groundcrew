"""auth: github identity, workspace members, approval decider

- users: github_id (upsert key) and avatar_url; email becomes optional.
- workspace_members: who can see a workspace. Existing owners are backfilled.
- approvals.decided_by: the deciding user's id (was free text). Old text values become NULL.
- run_steps.approval_id: links approval steps to their approval for the timeline.
- stop_reason: new value stopped_by_user.

Revision ID: 9efa9e99f279
Revises: 49d75cbbfe61
Create Date: 2026-10-06 17:32:57.287491
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9efa9e99f279"
down_revision: str | None = "49d75cbbfe61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_members",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.Enum("owner", "member", name="workspace_role"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_workspace_members_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_workspace_members_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace_members")),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_members_workspace_user"),
    )
    op.create_index(
        op.f("ix_workspace_members_user_id"), "workspace_members", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_workspace_members_workspace_id"),
        "workspace_members",
        ["workspace_id"],
        unique=False,
    )
    op.execute(
        "INSERT INTO workspace_members (id, workspace_id, user_id, role) "
        "SELECT gen_random_uuid(), id, owner_id, 'owner' FROM workspaces"
    )
    op.alter_column(
        "approvals",
        "decided_by",
        existing_type=sa.VARCHAR(length=200),
        type_=sa.Uuid(),
        existing_nullable=True,
        postgresql_using=(
            "CASE WHEN decided_by ~* "
            "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' "
            "THEN decided_by::uuid ELSE NULL END"
        ),
    )
    op.create_index(op.f("ix_approvals_decided_by"), "approvals", ["decided_by"], unique=False)
    op.create_foreign_key(
        op.f("fk_approvals_decided_by_users"),
        "approvals",
        "users",
        ["decided_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("users", sa.Column("github_id", sa.BigInteger(), nullable=True))
    op.add_column("users", sa.Column("avatar_url", sa.String(length=500), nullable=True))
    op.alter_column("users", "email", existing_type=sa.VARCHAR(length=320), nullable=True)
    op.create_unique_constraint(op.f("uq_users_github_id"), "users", ["github_id"])
    op.add_column("run_steps", sa.Column("approval_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_run_steps_approval_id_approvals"),
        "run_steps",
        "approvals",
        ["approval_id"],
        ["id"],
        ondelete="SET NULL",
    )
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE stop_reason ADD VALUE IF NOT EXISTS 'stopped_by_user'")


def downgrade() -> None:
    # Postgres cannot drop an enum value; stopped_by_user stays in stop_reason.
    op.execute("UPDATE runs SET stop_reason = NULL WHERE stop_reason = 'stopped_by_user'")
    op.drop_constraint(op.f("fk_run_steps_approval_id_approvals"), "run_steps", type_="foreignkey")
    op.drop_column("run_steps", "approval_id")
    op.drop_constraint(op.f("uq_users_github_id"), "users", type_="unique")
    op.alter_column("users", "email", existing_type=sa.VARCHAR(length=320), nullable=False)
    op.drop_column("users", "avatar_url")
    op.drop_column("users", "github_id")
    op.drop_constraint(op.f("fk_approvals_decided_by_users"), "approvals", type_="foreignkey")
    op.drop_index(op.f("ix_approvals_decided_by"), table_name="approvals")
    op.alter_column(
        "approvals",
        "decided_by",
        existing_type=sa.Uuid(),
        type_=sa.VARCHAR(length=200),
        existing_nullable=True,
    )
    op.drop_index(op.f("ix_workspace_members_workspace_id"), table_name="workspace_members")
    op.drop_index(op.f("ix_workspace_members_user_id"), table_name="workspace_members")
    op.drop_table("workspace_members")
    op.execute("DROP TYPE IF EXISTS workspace_role")
