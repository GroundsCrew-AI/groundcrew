"""Merge every source into one time-ordered list (the `timeline` evidence source)."""

from collections.abc import Iterable

from app.telemetry.evidence import Line


def timeline(sources: Iterable[list[Line]]) -> list[Line]:
    """All lines oldest first; the same line from two readers appears once."""
    return sorted({item for lines in sources for item in lines})
