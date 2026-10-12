"""What Builder asks the model for (strict JSON schemas, docs/prompts/phase4-rizwan.md, M4)."""

from typing import Literal

from pydantic import BaseModel, Field

# The kind of change a fix makes. "Same error after the same kind of fix" stops the run with
# stop_reason no_progress. Every choice field allows "other" (the client refuses one without).
FixKind = Literal[
    "system_packages",
    "base_image",
    "install_command",
    "build_step",
    "start_command",
    "copy_files",
    "environment",
    "other",
]


class DockerfileDraft(BaseModel):
    """write_dockerfile (Super, reasoning low)."""

    dockerfile: str = Field(description="The complete Dockerfile.")
    rationale: str = Field(description="One short paragraph: why this base image and these steps.")


class DockerfileFix(BaseModel):
    """fix_build (Super, reasoning on; escalated from attempt 4)."""

    dockerfile: str = Field(description="The complete corrected Dockerfile.")
    rationale: str = Field(description="One short paragraph: what failed and why this fixes it.")
    changed_lines: list[str] = Field(
        default_factory=list, description="The Dockerfile lines that were added or changed."
    )
    fix_kind: FixKind = Field(description="The kind of change this fix makes.")
