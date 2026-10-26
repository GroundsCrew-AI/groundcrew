"""What an agent reports about its host and the services in its configuration file, and how
that becomes resources on the Map.

The inventory holds facts that change rarely (runtime, image, ports, health check, what the
agent may do), so a change starts a discovery of the connection. Live state (running, restarts,
health results) comes with heartbeats and events, not here.

Resources: one `agent:host` (external id `agent:<connection id>`) and one `agent:service` per
configured service (`agent:<connection id>:service:<name>`), provider `agent`. The connection
id, not the agent's key, names them, so reinstalling the agent keeps the same resources.
Nothing here is a secret: the agent never reports environment values.
"""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import Connection
from app.discovery.types import ConnectorResult, DiscoveredResource

PROVIDER = "agent"
HOST_TYPE = "agent:host"
SERVICE_TYPE = "agent:service"
CONFIG_KEY = "inventory"
HASH_KEY = "inventory_hash"
MAX_SERVICES = 100

# kubernetes and ecs: the agent runs in that cluster (docs/agent-plan.md "A5 plan").
Runtime = Literal["docker", "compose", "systemd", "kubernetes", "ecs"]
Action = Literal["restart", "rollback", "deploy"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HostFacts(_Strict):
    hostname: str = Field(max_length=253)
    os: str = Field(max_length=64, examples=["linux"])
    arch: str = Field(max_length=32, examples=["amd64"])
    kernel: str | None = Field(default=None, max_length=128)
    distribution: str | None = Field(default=None, max_length=128, examples=["Ubuntu 24.04"])


class ServiceFacts(_Strict):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    runtime: Runtime
    # docker: container name; compose: "<project>/<service>"; systemd: unit name;
    # kubernetes: "<namespace>/<deployment>"; ecs: "<cluster>/<service>".
    target: str = Field(max_length=300)
    # Docker and Compose: the image reference the container runs and its digest, if known.
    image: str | None = Field(default=None, max_length=500)
    image_digest: str | None = Field(default=None, max_length=100)
    ports: list[str] = Field(default_factory=list, max_length=50, examples=[["8000/tcp"]])
    health_url: str | None = Field(default=None, max_length=500)
    # container | journald | a file path
    logs: str | None = Field(default=None, max_length=500)
    deploy_image: str | None = Field(default=None, max_length=500)
    allow: list[Action] = Field(default_factory=list)
    # The agent found it (container exists, unit is loaded). A configured service that is
    # missing is still reported, so the Map shows it.
    found: bool = True
    problem: str | None = Field(default=None, max_length=500)


class Inventory(_Strict):
    host: HostFacts
    services: list[ServiceFacts] = Field(default_factory=list, max_length=MAX_SERVICES)


def digest(inventory: Inventory) -> str:
    text = json.dumps(inventory.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


VERSION_KEY = "agent_version"


def location(runtime: str | None, target: str | None, hostname: str) -> str:
    """Where a service runs, as people name it: the agent's own host means nothing for Kubernetes
    and ECS (it is the agent's pod or task), so those name the namespace or the cluster."""
    if runtime in ("kubernetes", "ecs") and target and "/" in target:
        where = target.split("/", 1)[0]
        return f"ECS cluster {where}" if runtime == "ecs" else f"Kubernetes namespace {where}"
    return hostname


def host_id(conn: Connection) -> str:
    return f"agent:{conn.id}"


def service_id(conn: Connection, name: str) -> str:
    return f"agent:{conn.id}:service:{name}"


def to_resources(conn: Connection, inventory: Inventory) -> ConnectorResult:
    host = inventory.host
    out = ConnectorResult(
        resources=[
            DiscoveredResource(
                provider=PROVIDER,
                type=HOST_TYPE,
                external_id=host_id(conn),
                name=host.hostname,
                account=host.hostname,
                config={
                    "os": host.os,
                    "arch": host.arch,
                    "kernel": host.kernel,
                    "distribution": host.distribution,
                    "agent_version": (conn.config or {}).get(VERSION_KEY),
                },
            )
        ]
    )
    for s in inventory.services:
        out.resources.append(
            DiscoveredResource(
                provider=PROVIDER,
                type=SERVICE_TYPE,
                external_id=service_id(conn, s.name),
                name=s.name,
                account=host.hostname,
                config={
                    "host": host.hostname,
                    **s.model_dump(mode="json", exclude={"name"}),
                },
            )
        )
        if not s.found:
            out.notes.append(f"{s.name}: {s.problem or 'not found on ' + host.hostname}")
    return out


def stored(conn: Connection) -> Inventory | None:
    raw = (conn.config or {}).get(CONFIG_KEY)
    return Inventory.model_validate(raw) if raw else None
