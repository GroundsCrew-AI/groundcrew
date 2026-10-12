"""approvals.decided_via: who decided when it was not a signed-in user (dev self-approve).

Revision ID: 8b5d3f0e2c41
Revises: 7a4c2e9d1b30
Create Date: 2026-10-12 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8b5d3f0e2c41"
down_revision: str | None = "7a4c2e9d1b30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("approvals", sa.Column("decided_via", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("approvals", "decided_via")
