"""Commands for agents: an approved emergency action, signed by Groundcrew, run by the agent.

Groundcrew's command key is an Ed25519 key derived from APPROVAL_TOKEN_KEY (dev: SECRETS_KEY),
so no new secret is needed and every API and worker process has the same key. Agents learn its
public key from their heartbeat and pin it on first sight; a command signed by another key is
refused on the host. A rotated APPROVAL_TOKEN_KEY therefore means re-enrolling the agents.

A command's `payload` is the exact JSON text that was signed (the agent verifies those bytes,
then reads them), so no JSON canonicalization has to match across languages:

    {"v": 1, "id": ..., "agent_id": ..., "action": "restart", "service": ..., "plan_hash": ...,
     "params": {...}, "issued_at": ..., "expires_at": ...}

`params` holds a rollback's target: {"image": <local image id>, "ref": <its reference>}, or a
deploy's image and port. A deploy's secrets (registry login, env values) are not in the payload:
they are stored encrypted on the command and the agent fetches them once with a signed request.

The agent runs a command only if it is signed by the pinned key, not expired, not seen before,
and its configuration `allow`s the action for that service.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.config import Settings
from app.db.models import Agent, AgentCommand
from app.db.session import SessionFactory
from app.security.crypto import encrypt

COMMAND_VERSION = 1
# How long an agent may take to pick a command up.
COMMAND_TTL = timedelta(minutes=5)
# Actions an agent runs, as its configuration names them.
RESTART = "restart"
ROLLBACK = "rollback"
DEPLOY = "deploy"


class CommandKeyError(Exception):
    """No key to sign agent commands with."""


def _material(settings: Settings) -> bytes:
    if settings.approval_token_key is not None and settings.approval_token_key.get_secret_value():
        return settings.approval_token_key.get_secret_value().encode()
    if settings.app_env == "dev" and settings.secrets_key is not None:
        return settings.secrets_key.get_secret_value().encode()
    raise CommandKeyError("APPROVAL_TOKEN_KEY is not set, so commands for agents cannot be signed.")


def command_key(settings: Settings) -> Ed25519PrivateKey:
    seed = hmac.new(_material(settings), b"groundcrew agent command key v1", hashlib.sha256)
    return Ed25519PrivateKey.from_private_bytes(seed.digest())


def command_public_key(settings: Settings) -> str:
    raw = command_key(settings).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def issue(
    agent: Agent,
    *,
    action: str,
    service: str,
    plan_hash: str,
    run_id: uuid.UUID | None,
    settings: Settings,
    now: datetime | None = None,
    params: dict[str, str] | None = None,
    secrets: dict[str, Any] | None = None,
) -> AgentCommand:
    """A signed command, ready to add to the session."""
    now = now or datetime.now(UTC)
    command_id = uuid.uuid4()
    expires = now + COMMAND_TTL
    payload = json.dumps(
        {
            "v": COMMAND_VERSION,
            "id": str(command_id),
            "agent_id": str(agent.id),
            "action": action,
            "service": service,
            "plan_hash": plan_hash,
            "params": params or {},
            "issued_at": now.isoformat(),
            "expires_at": expires.isoformat(),
        },
        separators=(",", ":"),
    )
    signature = base64.b64encode(command_key(settings).sign(payload.encode())).decode()
    return AgentCommand(
        id=command_id,
        agent_id=agent.id,
        workspace_id=agent.workspace_id,
        run_id=run_id,
        action=action,
        service=service,
        payload=payload,
        signature=signature,
        expires_at=expires,
        status="queued",
        secret_encrypted=encrypt(json.dumps(secrets)) if secrets else None,
    )


async def wait_for_result(
    session_factory: SessionFactory,
    command_id: uuid.UUID,
    timeout: timedelta,
    poll_seconds: float = 1.0,
) -> AgentCommand | None:
    """The command once the agent reported it (succeeded or failed), or expired; None if it
    was removed. A command still unanswered at `timeout` is marked expired."""
    deadline = datetime.now(UTC) + timeout
    while datetime.now(UTC) < deadline:
        await asyncio.sleep(poll_seconds)
        async with session_factory() as session:
            found = await session.get(AgentCommand, command_id)
            if found is None or found.status in ("succeeded", "failed", "expired"):
                return found
    async with session_factory() as session:
        found = await session.get(AgentCommand, command_id, with_for_update=True)
        if found is not None and found.status in ("queued", "sent"):
            found.status, found.finished_at = "expired", datetime.now(UTC)
            found.result = "No result from the agent in time."
            found.secret_encrypted = None
            await session.commit()
        return found
