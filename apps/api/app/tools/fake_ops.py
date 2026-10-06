"""Fake ops tools for the Echo Ops toy agent. They touch nothing real."""

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from app.db.models import ToolPolicy
from app.tools.registry import ToolContext, tool

FAKE_SERVICES = ("api", "worker", "web")


class NoArgs(BaseModel):
    pass


class ServiceName(BaseModel):
    name: str = Field(description="Name of the service to restart, e.g. 'api'.")


@tool(
    name="get_current_time",
    description="Get the current UTC time.",
    args=NoArgs,
    policy=ToolPolicy.read,
)
async def get_current_time(_ctx: ToolContext, _args: NoArgs) -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@tool(
    name="list_fake_services",
    description="List the services running in the fake environment.",
    args=NoArgs,
    policy=ToolPolicy.read,
)
async def list_fake_services(_ctx: ToolContext, _args: NoArgs) -> str:
    return ", ".join(FAKE_SERVICES)


@tool(
    name="restart_fake_service",
    description="Restart one fake service by name. Needs human approval.",
    args=ServiceName,
    policy=ToolPolicy.write,
)
async def restart_fake_service(_ctx: ToolContext, args: ServiceName) -> str:
    if args.name not in FAKE_SERVICES:
        return f"error: no service named {args.name!r}"
    return f"restarted {args.name}"
