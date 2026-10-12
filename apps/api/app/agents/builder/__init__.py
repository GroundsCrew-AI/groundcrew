"""Builder (Phase 4, M4): Dockerfile, build, automatic fixes, and smoke test in the sandbox."""

from app.agents.builder.builder import (
    Builder,
    BuilderError,
    BuildOutcome,
    CallRecorder,
    app_port,
    is_transient,
)

__all__ = ["BuildOutcome", "Builder", "BuilderError", "CallRecorder", "app_port", "is_transient"]
