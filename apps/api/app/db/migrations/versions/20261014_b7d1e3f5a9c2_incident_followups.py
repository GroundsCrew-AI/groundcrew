"""incident_followups (fix PRs left after an incident resolved) and incidents.remembered_at.

Existing resolved incidents keep being found by incident memory (remembered_at = resolved_at),
except those whose applied fix PR is still open in their stored state: they get a follow-up
(open, or merged when the PR already merged) and stay out of memory until the tracker sees the
PR merged or closed. No GitHub call here; the tracker checks them on its next pass.

Revision ID: b7d1e3f5a9c2
Revises: a4f2c7e9b1d6
Create Date: 2026-10-14 11:00:00
"""

import json
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "b7d1e3f5a9c2"
down_revision: str | None = "a4f2c7e9b1d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("incidents", sa.Column("remembered_at", sa.DateTime(timezone=True)))
    op.execute(
        "UPDATE incidents SET remembered_at = resolved_at "
        "WHERE status = 'resolved' AND resolved_at IS NOT NULL"
    )
    op.create_table(
        "incident_followups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), server_default="fix_pr", nullable=False),
        sa.Column("state", sa.String(16), server_default="open", nullable=False),
        sa.Column("repo", sa.String(200), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("base", sa.String(200), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("triggering_commit", sa.String(64)),
        sa.Column("merge_commit", sa.String(64)),
        sa.Column("merged_at", sa.DateTime(timezone=True)),
        sa.Column("deployed_at", sa.DateTime(timezone=True)),
        sa.Column("deploy_url", sa.String(500)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("incident_id", "repo", "number", name="uq_incident_followups_pr"),
    )
    op.create_index("ix_incident_followups_incident_id", "incident_followups", ["incident_id"])
    op.create_index("ix_incident_followups_workspace_id", "incident_followups", ["workspace_id"])
    op.create_index("ix_incident_followups_state", "incident_followups", ["state"])
    _backfill()


def _triggering_commit(state: dict[str, Any]) -> str | None:
    change = (state.get("root_cause") or {}).get("triggering_change") or {}
    ref, kind = change.get("ref"), change.get("kind")
    if not ref or kind not in ("commit", "deploy"):
        return None
    for marker in state.get("changes") or []:
        commit = marker.get("commit")
        if kind == "commit" and commit and commit.startswith(ref):
            return str(commit)
        if kind == "deploy" and marker.get("ref") == ref and commit:
            return str(commit)
    return str(ref) if kind == "commit" else None


def _time(value: Any) -> datetime | None:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None


def _backfill() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, workspace_id, state FROM incidents "
            "WHERE status = 'resolved' AND is_test = false"
        )
    ).all()
    for incident_id, workspace_id, state in rows:
        state = state if isinstance(state, dict) else json.loads(state or "{}")
        held = False
        for rec in state.get("fixes") or []:
            pr, draft = rec.get("pull_request") or {}, rec.get("draft") or {}
            if rec.get("status") != "applied" or pr.get("state") not in ("open", "merged"):
                continue
            if not draft.get("installation_id") or not pr.get("number"):
                continue
            held = held or pr.get("state") == "open"
            bind.execute(
                sa.text(
                    "INSERT INTO incident_followups (id, incident_id, workspace_id, kind, state, "
                    "repo, number, url, base, installation_id, triggering_commit, merged_at) "
                    "VALUES (:id, :incident, :ws, 'fix_pr', :state, :repo, :number, :url, :base, "
                    ":installation, :sha, :merged_at) ON CONFLICT DO NOTHING"
                ),
                {
                    "id": uuid.uuid4(),
                    "incident": incident_id,
                    "ws": workspace_id,
                    "state": pr["state"],
                    "repo": pr.get("repo") or draft.get("repo"),
                    "number": int(pr["number"]),
                    "url": pr.get("url") or "",
                    "base": draft.get("base") or "main",
                    "installation": int(draft["installation_id"]),
                    "sha": _triggering_commit(state),
                    "merged_at": _time(pr.get("merged_at")),
                },
            )
        if held:
            bind.execute(
                sa.text("UPDATE incidents SET remembered_at = NULL WHERE id = :id"),
                {"id": incident_id},
            )


def downgrade() -> None:
    op.drop_table("incident_followups")
    op.drop_column("incidents", "remembered_at")
