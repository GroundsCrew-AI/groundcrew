"""Deploy contract: stop_reason no_progress, runs.state, run_steps.data.

Revision ID: 5e2b8c1f9a40
Revises: 3c1d9e4a7b20
Create Date: 2026-10-11 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "5e2b8c1f9a40"
down_revision: str | None = "3c1d9e4a7b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_STOP_REASONS = (
    "completed",
    "max_steps",
    "budget",
    "waiting_approval",
    "error",
    "stopped_by_user",
)


def upgrade() -> None:
    # Postgres 12+ allows this inside a transaction; the value is only used by later ones.
    op.execute("ALTER TYPE stop_reason ADD VALUE IF NOT EXISTS 'no_progress' AFTER 'budget'")
    op.add_column(
        "runs",
        sa.Column(
            "state",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )
    op.add_column(
        "run_steps",
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("run_steps", "data")
    op.drop_column("runs", "state")
    # Postgres cannot drop an enum value: recreate the type without it.
    op.execute("UPDATE runs SET stop_reason = 'error' WHERE stop_reason = 'no_progress'")
    op.execute("ALTER TYPE stop_reason RENAME TO stop_reason_old")
    values = ", ".join(f"'{v}'" for v in _OLD_STOP_REASONS)
    op.execute(f"CREATE TYPE stop_reason AS ENUM ({values})")
    op.execute(
        "ALTER TABLE runs ALTER COLUMN stop_reason TYPE stop_reason "
        "USING stop_reason::text::stop_reason"
    )
    op.execute("DROP TYPE stop_reason_old")
