#!/usr/bin/env bash
# Phase 4 live verification (docs/deploy-live.md): legacy-flask-app from GitHub to a live URL
# through POST /deploys on the running dev stack (make dev KIND=1).
#
#   scripts/live_phase4.sh kind            # a person approves on the Deploy page
#   scripts/live_phase4.sh kind ecs        # ecs: REAL AWS writes, only with an OK in the chat
#   SELF_APPROVE=1 scripts/live_phase4.sh kind   # dev only: the script approves its own deploy
#
# ECS runs are rolled back (an emergency approval) and cleaned up for that run afterwards.
set -euo pipefail
cd "$(dirname "$0")/.."
export DATABASE_URL="${DB_URL:-postgresql+asyncpg://groundcrew:groundcrew@localhost:5433/groundcrew}"
exec uv run --project apps/api python scripts/live_phase4.py "$@"
