"""incident_followups: change_in_repo follow-ups (no PR) and dismissal.

number, url and installation_id become nullable; dismissed_at and dismissed_by are added.
Idempotent (IF NOT EXISTS): a local database may already have them from an unmerged draft of
b7d1e3f5a9c2.

Revision ID: c3e8a1f4b6d7
Revises: b7d1e3f5a9c2
Create Date: 2026-10-14 13:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c3e8a1f4b6d7"
down_revision: str | None = "b7d1e3f5a9c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column in ("number", "url", "installation_id"):
        op.execute(f"ALTER TABLE incident_followups ALTER COLUMN {column} DROP NOT NULL")
    op.execute(
        "ALTER TABLE incident_followups ADD COLUMN IF NOT EXISTS dismissed_at "
        "TIMESTAMP WITH TIME ZONE"
    )
    op.execute("ALTER TABLE incident_followups ADD COLUMN IF NOT EXISTS dismissed_by UUID")
    op.execute(
        """
        DO $$ BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.table_constraints
            WHERE table_name = 'incident_followups'
              AND constraint_type = 'FOREIGN KEY'
              AND constraint_name = 'fk_incident_followups_dismissed_by_users'
          ) THEN
            ALTER TABLE incident_followups ADD CONSTRAINT fk_incident_followups_dismissed_by_users
              FOREIGN KEY (dismissed_by) REFERENCES users (id) ON DELETE SET NULL;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM incident_followups WHERE kind = 'change_in_repo'")
    op.execute("ALTER TABLE incident_followups DROP COLUMN IF EXISTS dismissed_by")
    op.execute("ALTER TABLE incident_followups DROP COLUMN IF EXISTS dismissed_at")
    for column in ("number", "url", "installation_id"):
        op.execute(f"ALTER TABLE incident_followups ALTER COLUMN {column} SET NOT NULL")
