"""Which assistant (MCP workspace token) a request is running for, if any (app/mcp_server.py).

The MCP server calls the REST routes in-process, in the same task, so the routes' own audit
events can say an assistant started them, and through which token.
"""

import contextvars
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class TokenContext:
    token_id: uuid.UUID
    token_name: str
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    role: str


current: contextvars.ContextVar[TokenContext | None] = contextvars.ContextVar(
    "groundcrew_assistant_token", default=None
)
