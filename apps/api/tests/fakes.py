"""Test helpers: the scripted FakeLLM (app.llm.fake). Tests never call the real API."""

from app.llm.fake import FakeLLM, FakeReply, reply, status_error, timeout_error, tool_call

__all__ = ["FakeLLM", "FakeReply", "reply", "status_error", "timeout_error", "tool_call"]
