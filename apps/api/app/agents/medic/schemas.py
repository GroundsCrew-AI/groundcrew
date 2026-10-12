"""Medic's structured model output (docs/phase5-interfaces.md).

`RootCause` is the `root_cause` task's JSON schema (Ultra, Super with DEV_MODE_NO_ULTRA). Every
enum allows `other` (the client refuses a schema without it) and output is normalized onto the
allowed values before validation. After the call, Medic drops every `evidence` entry whose line
is not in the evidence it collected, and every claim left with no evidence (the same idea as
the Phase 3 summary check), so what reaches the UI is cited.
"""

from typing import Literal

from pydantic import BaseModel, Field

ChangeKind = Literal["commit", "secret_change", "deploy", "config", "other"]
Confidence = Literal["high", "medium", "low", "other"]
FixKind = Literal[
    "pull_request",
    "ecs_rollback_service",
    "secret_rollback_version",
    "force_redeploy",
    "other",
]


class EvidenceRef(BaseModel):
    """One cited evidence line, copied from a telemetry reader's output."""

    source: str = Field(description="Reader and resource, e.g. ecs:checkout-worker")
    timestamp: str = Field(description="ISO 8601 timestamp from the evidence line")
    line: str = Field(description="The message part of the evidence line, verbatim")


class TriggeringChange(BaseModel):
    kind: ChangeKind
    # Commit SHA, secret ARN with version id, task definition ARN, Vercel deployment id.
    ref: str | None = None
    at: str | None = Field(default=None, description="ISO 8601 time of the change")
    description: str | None = None


class FixOption(BaseModel):
    """A fix the model suggests. Medic turns these into concrete, approvable fixes; it never
    runs one as written by the model."""

    kind: FixKind
    description: str
    # Resource the fix acts on: service name, secret name, repository path.
    target: str | None = None


class RootCause(BaseModel):
    summary: str = Field(description="One or two sentences a person reads first")
    root_cause: str = Field(description="What broke and why, citing the evidence")
    triggering_change: TriggeringChange | None = None
    affected_resources: list[str] = Field(default_factory=list, description="Resource names")
    evidence: list[EvidenceRef] = Field(default_factory=list)
    confidence: Confidence
    fix_options: list[FixOption] = Field(default_factory=list)
