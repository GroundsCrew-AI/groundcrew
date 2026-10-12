"""approvals.used_at: single use of an approved target action, enforced in the database.

Revision ID: 7a4c2e9d1b30
Revises: 5e2b8c1f9a40
Create Date: 2026-10-12 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a4c2e9d1b30"
down_revision: str | None = "5e2b8c1f9a40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("approvals", sa.Column("used_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("approvals", "used_at")
