"""incidents: is_test (test incidents stay out of Medic's memory).

Revision ID: a4f2c7e9b1d6
Revises: 9c6e4a1f3d52
Create Date: 2026-10-13 12:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4f2c7e9b1d6"
down_revision: str | None = "9c6e4a1f3d52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "incidents",
        sa.Column("is_test", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("incidents", "is_test")
