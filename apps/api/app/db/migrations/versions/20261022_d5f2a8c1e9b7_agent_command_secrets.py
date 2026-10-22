"""agent_commands.secret_encrypted: a deploy's registry login and env values, fetched once.

Revision ID: d5f2a8c1e9b7
Revises: c3e8f1a2b4d6
Create Date: 2026-10-22 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5f2a8c1e9b7"
down_revision: str | None = "c3e8f1a2b4d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_commands", sa.Column("secret_encrypted", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_commands", "secret_encrypted")
