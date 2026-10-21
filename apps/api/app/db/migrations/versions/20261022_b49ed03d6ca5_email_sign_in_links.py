"""auth_tokens.user_id optional: an email sign-in link can be for an address with no account.

Revision ID: b49ed03d6ca5
Revises: 11cd808b2d33
Create Date: 2026-10-22 09:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b49ed03d6ca5"
down_revision: str | None = "11cd808b2d33"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("auth_tokens", "user_id", nullable=True)
    op.create_index("ix_auth_tokens_email_purpose", "auth_tokens", ["email", "purpose"])


def downgrade() -> None:
    op.drop_index("ix_auth_tokens_email_purpose", table_name="auth_tokens")
    op.execute("DELETE FROM auth_tokens WHERE user_id IS NULL")
    op.alter_column("auth_tokens", "user_id", nullable=False)
