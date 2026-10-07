import os

import asyncpg
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/items")
async def items() -> list[dict[str, object]]:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        rows = await conn.fetch("SELECT id, name FROM items ORDER BY id")
        return [dict(row) for row in rows]
    finally:
        await conn.close()
