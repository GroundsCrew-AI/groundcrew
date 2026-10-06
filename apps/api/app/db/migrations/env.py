"""Alembic environment. Runs migrations with the async engine from DATABASE_URL."""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from app.db.models import Base
from app.db.session import create_engine, get_engine

target_metadata = Base.metadata


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    # Tests pass their own URL; otherwise use DATABASE_URL from settings.
    url = context.config.attributes.get("database_url")
    engine = create_engine(url) if url else get_engine()
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline migrations are not supported. Run against a database.")

asyncio.run(_run_async_migrations())
