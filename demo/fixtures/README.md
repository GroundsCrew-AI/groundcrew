# Scout fixtures

Tiny apps for testing Scout (and later Builder and Deployer). Each fixture has the app in `repo/` and the
golden AppSpec in `expected_appspec.json` next to it, outside `repo/`, so Scout never sees the answer.

| Fixture | What it checks |
|---|---|
| `next-app` | Next.js + TypeScript, port 3000 by default, no database, no env vars |
| `fastapi-pg` | FastAPI app object to a uvicorn command, `DATABASE_URL`, `/health` |
| `express-redis` | Express with `REDIS_URL` (required, secret) and `PORT` (has a default, so optional), `/healthz` |
| `py-build-trap` | Flask + gunicorn + `psycopg2` (built from source): the obvious slim Dockerfile fails without `libpq-dev` and a compiler. Builder must fix it. |

Golden tests: `apps/api/tests/test_scout.py`. Real Nano accuracy: `scripts/live_scout.py`.
