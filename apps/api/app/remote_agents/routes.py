"""The agent API (`/agent/v1`): enrollment, heartbeats, inventory, service states and events,
and approved commands (a long poll, then the result).

Agents call it directly at AGENT_API_URL (nginx passes `/agent/` through; every other API route
is reached only through the web app). No user signs in here: enrollment takes a one-time join
token, and every other call must be signed by the enrolled agent's key (signing.py). A revoked
connection deletes its agent, so its key stops working at once.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record
from app.db.models import Agent, AgentCommand, AgentEvent, AgentNonce, Connection
from app.discovery.engine import start_discovery
from app.remote_agents import commands as cmd
from app.remote_agents import inventory as inv
from app.remote_agents import signals as sig
from app.remote_agents.signing import (
    NONCE,
    SignatureError,
    canonical,
    public_key,
    token_hash,
    verify,
)
from app.routes.deps import SessionDep, SessionFactoryDep, SettingsDep, too_many_requests
from app.security.ratelimit import limiter

router = APIRouter(prefix="/agent/v1", tags=["agent"])

HEARTBEAT_SECONDS = 30
INVENTORY_SECONDS = 60
EVENTS_SECONDS = 10
# A command poll waits up to this long for a command, checking every POLL_CHECK_SECONDS.
POLL_SECONDS = 25
POLL_CHECK_SECONDS = 1.0
_ENROLLS_PER_MINUTE = 10


class EnrollIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    public_key: str = Field(max_length=64, description="Base64 of the raw Ed25519 public key")
    hostname: str = Field(max_length=253)
    os: str = Field(max_length=64)
    arch: str = Field(max_length=32)
    version: str = Field(max_length=32)


class EnrollOut(BaseModel):
    agent_id: uuid.UUID
    connection_id: uuid.UUID
    name: str
    heartbeat_seconds: int = HEARTBEAT_SECONDS
    inventory_seconds: int = INVENTORY_SECONDS


class HeartbeatIn(BaseModel):
    version: str = Field(max_length=32)
    uptime_seconds: int = Field(ge=0)
    config_hash: str | None = Field(default=None, max_length=64)


class HeartbeatOut(BaseModel):
    heartbeat_seconds: int = HEARTBEAT_SECONDS
    events_seconds: int = EVENTS_SECONDS
    # The inventory Groundcrew holds; the agent sends its own when the two differ.
    inventory_hash: str | None = None
    # Groundcrew's command-signing public key (base64 Ed25519); the agent pins it on first sight.
    command_key: str | None = None


class EventsOut(BaseModel):
    stored: int


class CommandOut(BaseModel):
    """A command to run: `payload` is the exact signed JSON text, `signature` its base64
    Ed25519 signature by Groundcrew's command key."""

    id: uuid.UUID
    payload: str
    signature: str


class PollOut(BaseModel):
    commands: list[CommandOut] = Field(default_factory=list)


class CommandResultIn(BaseModel):
    ok: bool
    message: str = Field(max_length=2000)


class InventoryOut(BaseModel):
    changed: bool
    discovery_run_id: uuid.UUID | None = None


def _refused(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail)


def _address(request: Request) -> str:
    """The agent's address as nginx saw it (it overwrites X-Real-IP)."""
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "-")


async def signed_agent(request: Request, session: SessionDep, settings: SettingsDep) -> Agent:
    """The enrolled agent that signed this request, or 401."""
    h = request.headers
    agent_id, stamp, nonce, signature = (
        h.get("x-groundcrew-agent"),
        h.get("x-groundcrew-timestamp", ""),
        h.get("x-groundcrew-nonce", ""),
        h.get("x-groundcrew-signature", ""),
    )
    try:
        parsed = uuid.UUID(agent_id or "")
        sent = datetime.fromtimestamp(int(stamp), UTC)
    except ValueError:
        raise _refused("Sign the request: agent id and timestamp headers are missing.") from None
    if not NONCE.match(nonce):
        raise _refused("Sign the request: the nonce header is missing or malformed.")
    now = datetime.now(UTC)
    if abs((now - sent).total_seconds()) > settings.agent_request_skew_seconds:
        raise _refused(
            f"The request's clock is off by more than {settings.agent_request_skew_seconds} "
            "seconds; check the host's time (NTP)."
        )
    agent = await session.get(Agent, parsed)
    if agent is None or agent.public_key is None:
        raise _refused("This agent is not enrolled here (or its connection was removed).")
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    try:
        verify(
            agent.public_key,
            signature,
            canonical(request.method, path, stamp, nonce, await request.body()),
        )
    except SignatureError as exc:
        raise _refused(str(exc)) from None
    # After the signature, so nobody else can use up this agent's allowance.
    if not limiter.allow(f"agent:{agent.id}", settings.agent_rate_limit_per_minute, 60):
        raise too_many_requests()
    added = await session.execute(
        insert(AgentNonce)
        .values(agent_id=agent.id, nonce=nonce, seen_at=now)
        .on_conflict_do_nothing()
        .returning(AgentNonce.nonce)
    )
    if added.scalar_one_or_none() is None:
        raise _refused("This request was already received (replayed nonce).")
    # Older nonces can no longer be replayed (the timestamp check refuses them).
    await session.execute(
        delete(AgentNonce).where(
            AgentNonce.agent_id == agent.id,
            AgentNonce.seen_at < now - timedelta(seconds=2 * settings.agent_request_skew_seconds),
        )
    )
    agent.last_seen_at = now
    return agent


SignedAgent = Annotated[Agent, Depends(signed_agent)]


async def _connection(session: AsyncSession, agent: Agent) -> Connection:
    conn = await session.get(Connection, agent.connection_id, with_for_update=True)
    if conn is None:
        raise _refused("This agent's connection was removed.")
    return conn


@router.post("/enroll")
async def enroll(
    body: EnrollIn, request: Request, session: SessionDep, settings: SettingsDep
) -> EnrollOut:
    """Trade a one-time join token for this agent's place in the workspace. A new token for an
    enrolled agent (a reinstall) replaces its key."""
    ip = _address(request)
    if not limiter.allow(f"agent:enroll:{ip}", _ENROLLS_PER_MINUTE, 60):
        raise too_many_requests()
    try:
        key = public_key(body.public_key)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    agent = await session.scalar(
        select(Agent).where(Agent.join_token_hash == token_hash(body.token)).with_for_update()
    )
    now = datetime.now(UTC)
    if agent is None or agent.join_expires_at is None or agent.join_expires_at < now:
        raise _refused(
            "This join token is not valid: it was used already, has expired, or was replaced. "
            "Create a new one on the Connections page."
        )
    conn = await _connection(session, agent)
    agent.join_token_hash, agent.join_expires_at = None, None
    agent.public_key = key
    agent.hostname, agent.os, agent.arch, agent.version = (
        body.hostname,
        body.os,
        body.arch,
        body.version,
    )
    agent.enrolled_at = agent.last_seen_at = now
    await session.execute(delete(AgentNonce).where(AgentNonce.agent_id == agent.id))
    conn.status = "connected"
    conn.config = {**(conn.config or {}), "hostname": body.hostname, inv.VERSION_KEY: body.version}
    record(
        session,
        workspace_id=agent.workspace_id,
        actor=None,
        action="connection.agent_enrolled",
        target_type="connection",
        target_id=conn.id,
        detail={"kind": conn.kind, "name": conn.name, "hostname": body.hostname},
        ip=ip,
    )
    await session.commit()
    return EnrollOut(agent_id=agent.id, connection_id=conn.id, name=conn.name)


@router.post("/heartbeat")
async def heartbeat(
    body: HeartbeatIn, agent: SignedAgent, session: SessionDep, settings: SettingsDep
) -> HeartbeatOut:
    agent.version = body.version
    conn = await _connection(session, agent)
    if (conn.config or {}).get(inv.VERSION_KEY) != body.version:
        conn.config = {**(conn.config or {}), inv.VERSION_KEY: body.version}
    held = (conn.config or {}).get(inv.HASH_KEY)
    await session.commit()
    try:
        key: str | None = cmd.command_public_key(settings)
    except cmd.CommandKeyError:
        key = None
    return HeartbeatOut(inventory_hash=held, command_key=key)


@router.post("/inventory")
async def inventory(body: inv.Inventory, agent: SignedAgent, session: SessionDep) -> InventoryOut:
    """The services the agent watches. A change is stored and starts a discovery of this
    connection, which puts them on the Map (and marks services no longer listed as stale)."""
    conn = await _connection(session, agent)
    new = inv.digest(body)
    if (conn.config or {}).get(inv.HASH_KEY) == new:
        await session.commit()
        return InventoryOut(changed=False)
    conn.config = {
        **(conn.config or {}),
        inv.CONFIG_KEY: body.model_dump(mode="json"),
        inv.HASH_KEY: new,
        "hostname": body.host.hostname,
    }
    agent.hostname = body.host.hostname
    run = await start_discovery(session, agent.workspace_id, connection=conn)
    await session.commit()
    return InventoryOut(changed=True, discovery_run_id=run.id)


@router.post("/events")
async def events(
    body: sig.EventsIn, agent: SignedAgent, session: SessionDep, settings: SettingsDep
) -> EventsOut:
    """The services' live state and what happened since the last report. A detector rule that
    starts firing for a service opens (or joins) an incident on it."""
    conn = await _connection(session, agent)
    started = await sig.ingest(session, agent, conn, body, settings)
    if started:
        await sig.open_incidents(session, agent.workspace_id, started)
    return EventsOut(stored=len(body.events))


@router.post("/commands/poll")
async def poll_commands(
    agent: SignedAgent, session: SessionDep, sessions: SessionFactoryDep
) -> PollOut:
    """Approved commands for this agent, waiting up to POLL_SECONDS for one. Expired commands
    are marked so; a command is handed out once (status sent)."""
    agent_id = agent.id
    await session.commit()  # the agent's request is checked; hold no connection while waiting
    deadline = datetime.now(UTC) + timedelta(seconds=POLL_SECONDS)
    while True:
        now = datetime.now(UTC)
        async with sessions() as s:
            queued = list(
                await s.scalars(
                    select(AgentCommand)
                    .where(AgentCommand.agent_id == agent_id, AgentCommand.status == "queued")
                    .order_by(AgentCommand.created_at)
                    .with_for_update(skip_locked=True)
                )
            )
            out = []
            for c in queued:
                if c.expires_at <= now:
                    c.status, c.finished_at, c.result = "expired", now, "Not picked up in time."
                    continue
                c.status, c.sent_at = "sent", now
                out.append(CommandOut(id=c.id, payload=c.payload, signature=c.signature))
            await s.commit()
        if out or now >= deadline:
            return PollOut(commands=out)
        await asyncio.sleep(POLL_CHECK_SECONDS)


@router.post("/commands/{command_id}/result")
async def command_result(
    command_id: uuid.UUID, body: CommandResultIn, agent: SignedAgent, session: SessionDep
) -> EventsOut:
    """What the agent did with a command (ran it, or refused it and why)."""
    found = await session.get(AgentCommand, command_id, with_for_update=True)
    if found is None or found.agent_id != agent.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such command for this agent.")
    if found.status == "sent":
        found.status = "succeeded" if body.ok else "failed"
        found.finished_at = datetime.now(UTC)
        found.result = body.message[:2000]
        session.add(
            AgentEvent(
                agent_id=agent.id,
                service=found.service,
                at=found.finished_at,
                kind="command",
                message=f"{found.action} {'done' if body.ok else 'failed'}: {body.message}"[:500],
            )
        )
    await session.commit()
    return EventsOut(stored=1)
