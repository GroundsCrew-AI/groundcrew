"""Groundcrew agents on hosts (connection kind `agent`; docs/agent-plan.md).

`signing` (join tokens, signed requests), `inventory` (what an agent reports, as resources), and
`routes` (the `/agent/v1` API agents call). The agent itself is apps/agent (Go).
"""

# The agent version groundkrew.com/agent/ serves (apps/agent/VERSION; a test keeps them equal).
# An agent reporting an older one is shown as out of date.
AGENT_VERSION = "0.6.1"


def version_tuple(version: str | None) -> tuple[int, ...] | None:
    """(0, 3, 0) from "0.3.0" or "0.3.0-dev"; None for "dev" or anything unparsable."""
    core = (version or "").split("-", 1)[0]
    parts = core.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def outdated(version: str | None, needed: str = AGENT_VERSION) -> bool:
    have, want = version_tuple(version), version_tuple(needed)
    return have is not None and want is not None and have < want
