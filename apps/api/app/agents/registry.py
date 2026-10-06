"""Agents that runs can use, by name."""

from app.agents.base import Agent, UnknownAgentError
from app.agents.toy import ECHO_OPS

AGENTS: dict[str, Agent] = {agent.name: agent for agent in (ECHO_OPS,)}


def get_agent(name: str) -> Agent:
    try:
        return AGENTS[name]
    except KeyError:
        raise UnknownAgentError(name) from None
