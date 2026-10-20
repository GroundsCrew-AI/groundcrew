# Scout fixtures

Tiny apps for testing Scout (and later Builder and Deployer). Each fixture has the app in `repo/` and the
golden AppSpec in `expected_appspec.json` next to it, outside `repo/`, so Scout never sees the answer.

| Fixture | What it checks |
|---|---|
| `next-app` | Next.js + TypeScript, port 3000 by default, no database, no env vars |
| `fastapi-pg` | FastAPI app object to a uvicorn command, `DATABASE_URL`, `/health` |
| `express-redis` | Express with `REDIS_URL` (required, secret) and `PORT` (has a default, so optional), `/healthz` |
| `py-build-trap` | Flask + gunicorn + `psycopg2` (built from source): the obvious slim Dockerfile fails without `libpq-dev` and a compiler. Builder must fix it. |

| `java-spring-maven` | Spring Boot on Maven: Java 21, `java -jar target/orders-0.0.1-SNAPSHOT.jar`, 8080, Actuator `/actuator/health`, postgres driver |
| `ruby-rails` | Rails: Ruby from the Gemfile, `rails server` on 3000, `pg`, the `/up` health route |
| `rust-axum` | axum: `rust-version`, `./target/release/ledger`, 3000, sqlx with the postgres feature |
| `php-laravel` | Laravel: PHP from composer.json, php-apache (`apache2-foreground`) on 80, predis, `health: '/up'` |
| `dotnet-aspnet` | ASP.NET Core 8 in a solution (`src/Weather.Api`): restore/publish commands, 8080, Npgsql, `MapHealthChecks("/healthz")` |

The last five are manifests only (Builder variants of each ecosystem: `apps/api/tests/test_builder_languages.py`).

Golden tests: `apps/api/tests/test_scout.py`. Real Nano accuracy: `scripts/live_scout.py`.
