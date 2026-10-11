"""Alembic has exactly one head: two branches that both add a migration must be merged (or
rebased) before they land, or `alembic upgrade head` fails. CI also checks `alembic heads`."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

API_DIR = Path(__file__).resolve().parents[1]


def test_one_alembic_head() -> None:
    script = ScriptDirectory.from_config(Config(str(API_DIR / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1, f"Alembic has {len(heads)} heads: {heads}. Merge them first."
