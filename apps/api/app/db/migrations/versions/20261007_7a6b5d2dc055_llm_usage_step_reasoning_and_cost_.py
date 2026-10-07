"""LLM usage table and step reasoning and cost columns.

- llm_usage: Token Factory spend per UTC day; the daily and total USD caps read it. Backfilled
  from run_steps, so earlier spend counts (cost is 0 where no price was set back then).
- run_steps: reasoning_mode, ultra_equiv_usd, fell_back, escalated, reasoning_excerpt.
- runs: ultra_equiv_usd.

Revision ID: 7a6b5d2dc055
Revises: 9efa9e99f279
Create Date: 2026-10-07 12:31:50.290767
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a6b5d2dc055"
down_revision: str | None = "9efa9e99f279"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("usd", sa.Numeric(precision=12, scale=6), server_default="0", nullable=False),
        sa.Column("tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("day", name=op.f("pk_llm_usage")),
    )
    op.add_column("run_steps", sa.Column("reasoning_mode", sa.String(length=8), nullable=True))
    op.add_column(
        "run_steps", sa.Column("ultra_equiv_usd", sa.Numeric(precision=12, scale=6), nullable=True)
    )
    op.add_column(
        "run_steps", sa.Column("fell_back", sa.Boolean(), server_default="false", nullable=False)
    )
    op.add_column(
        "run_steps", sa.Column("escalated", sa.Boolean(), server_default="false", nullable=False)
    )
    op.add_column("run_steps", sa.Column("reasoning_excerpt", sa.Text(), nullable=True))
    op.add_column(
        "runs", sa.Column("ultra_equiv_usd", sa.Numeric(precision=12, scale=6), nullable=True)
    )
    op.execute(
        """
        INSERT INTO llm_usage (day, usd, tokens, calls)
        SELECT (created_at AT TIME ZONE 'UTC')::date,
               COALESCE(SUM(cost_usd), 0),
               COALESCE(SUM(COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0)), 0),
               COUNT(*)
        FROM run_steps
        WHERE kind = 'llm'
        GROUP BY 1
        """
    )


def downgrade() -> None:
    op.drop_column("runs", "ultra_equiv_usd")
    op.drop_column("run_steps", "reasoning_excerpt")
    op.drop_column("run_steps", "escalated")
    op.drop_column("run_steps", "fell_back")
    op.drop_column("run_steps", "ultra_equiv_usd")
    op.drop_column("run_steps", "reasoning_mode")
    op.drop_table("llm_usage")
