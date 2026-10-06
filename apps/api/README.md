# apps/api

Groundcrew control plane: FastAPI, Python 3.12, managed with uv.

```bash
uv sync
make -C ../.. db migrate seed            # Postgres in compose, tables, Demo workspace
DATABASE_URL=postgresql+asyncpg://groundcrew:groundcrew@localhost:5433/groundcrew \
  uv run uvicorn app.main:app --reload   # http://localhost:8000/docs
make -C ../.. worker                     # runs agent jobs
uv run pytest                            # needs Postgres (make db); LLM is faked
uv run ruff check . && uv run ruff format --check . && uv run mypy app tests
```

New migration after changing `app/db/models.py`:
`DATABASE_URL=... uv run alembic revision --autogenerate -m "what changed"`, then review the file.

Layout: `db/` models, session, migrations · `llm/` client, router, costs, budget · `tools/` registry and tools ·
`policy/engine.py` approval gate · `agents/` loop and agents · `routes/` REST and SSE · `queue.py`, `worker.py`.
