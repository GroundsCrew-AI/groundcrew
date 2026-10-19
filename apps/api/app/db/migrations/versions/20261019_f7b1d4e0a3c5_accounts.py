"""Accounts beyond GitHub: email and password, linked sign-in identities, link tokens, and
invites by email.

- users: email_verified_at, password_hash, sessions_valid_after, failed_sign_ins, locked_until.
- user_identities: one row per provider account (backfilled from users.github_id).
- auth_tokens: single-use email verification and password reset tokens (hashes only).
- workspace_invites: email next to github_login; one of the two is set.

Revision ID: f7b1d4e0a3c5
Revises: e6a0c3d9f2b4
Create Date: 2026-10-19 10:00:00
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision: str = "f7b1d4e0a3c5"
down_revision: str | None = "e6a0c3d9f2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column[datetime]]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("password_hash", sa.String(255)))
    op.add_column("users", sa.Column("sessions_valid_after", sa.DateTime(timezone=True)))
    op.add_column(
        "users",
        sa.Column("failed_sign_ins", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("users", sa.Column("locked_until", sa.DateTime(timezone=True)))

    op.create_table(
        "user_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("provider_account_id", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320)),
        sa.Column("login", sa.String(100)),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
            name="fk_user_identities_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_identities"),
        sa.UniqueConstraint("provider", "provider_account_id", name="uq_user_identities_account"),
        sa.UniqueConstraint("user_id", "provider", name="uq_user_identities_user_provider"),
    )
    op.create_index("ix_user_identities_user_id", "user_identities", ["user_id"])
    # Everyone who signed in so far did it with GitHub.
    op.execute(
        """
        INSERT INTO user_identities (id, user_id, provider, provider_account_id, email, login)
        SELECT gen_random_uuid(), id, 'github', github_id::text, email, github_login
        FROM users WHERE github_id IS NOT NULL
        """
    )

    op.create_table(
        "auth_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(20), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE", name="fk_auth_tokens_user_id_users"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_auth_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_auth_tokens_token_hash"),
    )
    op.create_index("ix_auth_tokens_user_id", "auth_tokens", ["user_id"])

    op.alter_column("workspace_invites", "github_login", nullable=True)
    op.add_column("workspace_invites", sa.Column("email", sa.String(320)))
    op.create_index("ix_workspace_invites_email", "workspace_invites", ["email"])
    op.create_unique_constraint(
        "uq_workspace_invites_email", "workspace_invites", ["workspace_id", "email"]
    )
    op.create_check_constraint(
        "ck_workspace_invites_target",
        "workspace_invites",
        "github_login IS NOT NULL OR email IS NOT NULL",
    )


def downgrade() -> None:
    op.execute("DELETE FROM workspace_invites WHERE github_login IS NULL")
    op.drop_constraint("ck_workspace_invites_target", "workspace_invites", type_="check")
    op.drop_constraint("uq_workspace_invites_email", "workspace_invites", type_="unique")
    op.drop_index("ix_workspace_invites_email", table_name="workspace_invites")
    op.drop_column("workspace_invites", "email")
    op.alter_column("workspace_invites", "github_login", nullable=False)

    op.drop_index("ix_auth_tokens_user_id", table_name="auth_tokens")
    op.drop_table("auth_tokens")
    op.drop_index("ix_user_identities_user_id", table_name="user_identities")
    op.drop_table("user_identities")

    for column in (
        "locked_until",
        "failed_sign_ins",
        "sessions_valid_after",
        "password_hash",
        "email_verified_at",
    ):
        op.drop_column("users", column)
