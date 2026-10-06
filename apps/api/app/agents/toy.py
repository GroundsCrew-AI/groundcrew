"""Echo Ops: a toy agent on the Nano tier with fake tools, for testing the loop end to end.

Example goal: "List the services and restart the one named api."
"""

from app.agents.base import Agent
from app.llm.router import TaskType
from app.tools import fake_ops  # noqa: F401  (registers the fake tools)

ECHO_OPS = Agent(
    name="echo_ops",
    system_prompt=(
        "You are Echo Ops, a test agent working on a fake environment. "
        "Use the tools to do what the user asks, one tool call at a time. "
        "Do not ask the user to confirm anything in chat: call the tool. Actions that need "
        "human approval, such as restart_fake_service, are paused for approval by the system "
        "before they run. If an action is rejected, do not retry it, and say in your summary that "
        "the user rejected it. "
        "When you are done, reply with a short plain summary of what you did."
    ),
    tools=("get_current_time", "list_fake_services", "restart_fake_service"),
    task_type=TaskType.summarize,  # Nano tier
)
