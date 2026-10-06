# apps/api

Groundcrew control plane: FastAPI, Python 3.12, managed with uv.

```bash
uv sync
uv run uvicorn app.main:app --reload   # http://localhost:8000/health
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy app tests
```
