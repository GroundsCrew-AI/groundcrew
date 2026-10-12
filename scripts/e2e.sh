#!/usr/bin/env bash
# Playwright end-to-end tests in fake mode (M8): the real web app, API, worker, and pipeline,
# with scripted model answers, a scripted sandbox, and an in-memory cluster. No Token Factory
# key, no Docker builds, no cluster. CI runs this; locally:
#
#   scripts/e2e.sh                 # uses the compose Postgres (make db), database groundcrew_e2e
#   E2E_DB_URL=postgresql+asyncpg://... scripts/e2e.sh
#
# Ports: API 8100, web 3100 (E2E_API_PORT, E2E_WEB_PORT). Logs in .e2e/.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
API_PORT=${E2E_API_PORT:-8100}
WEB_PORT=${E2E_WEB_PORT:-3100}
DB_URL=${E2E_DB_URL:-postgresql+asyncpg://groundcrew:groundcrew@localhost:5433/groundcrew_e2e}
mkdir -p .e2e

# Fresh, dev-only settings. Nothing here is a real secret.
export DATABASE_URL=$DB_URL
export APP_ENV=dev PIPELINE_FAKE=1 DEV_MODE_NO_ULTRA=true LIVE_SCRIPT_SELF_APPROVE=
export INTERNAL_JWT_SECRET=e2e-internal-jwt-secret-0123456789abcdef
export APPROVAL_TOKEN_KEY=e2e-approval-token-key-0123456789abcdef
export SECRETS_KEY=${E2E_SECRETS_KEY:-$(cd apps/api && uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")}
export TOKEN_FACTORY_BASE_URL=http://token-factory.invalid/v1 TOKEN_FACTORY_API_KEY=fake
export MODEL_NANO=fake/nano MODEL_SUPER=fake/super MODEL_ULTRA=fake/ultra
export WEB_ORIGIN=http://localhost:$WEB_PORT SSE_POLL_INTERVAL_MS=200 WORKER_POLL_INTERVAL_SECONDS=0.3

pids=()
cleanup() { for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done; }
trap cleanup EXIT

echo "Database: create if missing, migrate, seed"
(cd apps/api && uv run python - "$DB_URL" <<'PY'
import asyncio, sys
import asyncpg
from sqlalchemy.engine import make_url
url = make_url(sys.argv[1])
async def main() -> None:
    conn = await asyncpg.connect(host=url.host, port=url.port, user=url.username,
                                 password=url.password, database="postgres")
    if not await conn.fetchval("select 1 from pg_database where datname = $1", url.database):
        await conn.execute(f'create database "{url.database}"')
    await conn.close()
asyncio.run(main())
PY
)
(cd apps/api && uv run alembic upgrade head >/dev/null)
uv run --project apps/api python scripts/e2e_seed.py

echo "API on :$API_PORT, worker"
(cd apps/api && exec uv run uvicorn app.main:app --port "$API_PORT" >"$ROOT/.e2e/api.log" 2>&1) & pids+=($!)
(cd apps/api && exec uv run python -m app.worker >"$ROOT/.e2e/worker.log" 2>&1) & pids+=($!)

echo "Web on :$WEB_PORT (build)"
export NEXT_PUBLIC_API_URL=http://localhost:$API_PORT API_INTERNAL_URL=http://localhost:$API_PORT
export AUTH_SECRET=e2e-auth-secret-0123456789abcdef0123456789 AUTH_TRUST_HOST=true E2E_TEST_AUTH=1
(cd apps/web && npm run build >"$ROOT/.e2e/web-build.log" 2>&1)
(cd apps/web && exec npx next start -p "$WEB_PORT" >"$ROOT/.e2e/web.log" 2>&1) & pids+=($!)

for url in "http://localhost:$API_PORT/health" "http://localhost:$WEB_PORT/signin"; do
  for _ in $(seq 1 60); do curl -fsS -o /dev/null "$url" && break; sleep 1; done
  curl -fsS -o /dev/null "$url" || { echo "not up: $url"; tail -30 .e2e/*.log; exit 1; }
done

echo "Playwright"
(cd apps/web && E2E_BASE_URL=http://localhost:$WEB_PORT npx playwright test "$@")
