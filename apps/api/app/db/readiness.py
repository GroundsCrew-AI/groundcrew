"""Readiness for /ready: the database answers, and its schema is at this build's Alembic head
(a deploy that forgot `alembic upgrade head` reports `migrations: behind`)."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.db.session import get_engine

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def expected_head() -> str | None:
    return ScriptDirectory.from_config(Config(str(_ALEMBIC_INI))).get_current_head()


async def check() -> tuple[str, str]:
    """(database, migrations): each "ok", or a short reason."""
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
            current = (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
    except Exception as exc:  # any connection or SQL error means not ready
        return f"unavailable ({type(exc).__name__})", "unknown"
    head = expected_head()
    if current is None:
        return "ok", "not migrated"
    return "ok", "ok" if current == head else f"behind ({current}, head {head})"
