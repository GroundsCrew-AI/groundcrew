"""The short facts on a Map node (routes/discovery_schemas.py _facts)."""

from app.db.models import Resource
from app.routes.discovery_schemas import _facts


def test_missing_values_are_left_out() -> None:
    db = Resource(type="rds:db", config={"engine": "postgres"})
    assert _facts(db) == ["postgres", "private"]
    db = Resource(type="rds:db", config={"engine": "postgres", "engine_version": "16.4"})
    assert _facts(db) == ["postgres 16.4", "private"]
