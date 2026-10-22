"""Medic's evidence items and structured model output (docs/phase5-interfaces.md).

Evidence lines get stable ids (`evidence_id`), and the model cites them by id: every claim in
`RootCause` lists the ids it rests on. After the call Medic drops every id that is not in the
evidence it collected, and every claim (and triggering change) left with no valid id, the same
idea as the Phase 3 summary check. What reaches the UI is cited, and the UI links each claim to
its lines.

`RootCause` is the `root_cause` task's JSON schema (Ultra, Super with DEV_MODE_NO_ULTRA). Every
enum allows `other` (the client refuses a schema without it) and output is normalized onto the
allowed values before validation.
"""

import hashlib
import uuid
from typing import Literal

from pydantic import BaseModel, Field

ChangeKind = Literal["commit", "secret_change", "deploy", "config", "other"]
Confidence = Literal["high", "medium", "low", "other"]
FixOptionKind = Literal[
    "pull_request",
    "ecs_rollback_service",
    "secret_rollback_version",
    "force_redeploy",
    "lambda_rollback_alias",
    "ec2_reboot_instance",
    "k8s_rollout_undo",
    "k8s_rollout_restart",
    "agent_restart_service",
    "agent_rollback_service",
    "other",
]


def evidence_id(source: str, timestamp: str, line: str) -> str:
    """Stable id for an evidence line: the same line always gets the same id."""
    digest = hashlib.sha256(f"{source}|{timestamp}|{line}".encode()).hexdigest()
    return f"ev-{digest[:10]}"


class EvidenceItem(BaseModel):
    """One evidence line, as collected (shown to the model with its id, and to the UI)."""

    id: str = Field(examples=["ev-3f2a9c1b7d"])
    source: str = Field(examples=["ecs:checkout-worker", "cloudwatch_logs:/ecs/checkout-api"])
    timestamp: str = Field(description="ISO 8601")
    resource_id: uuid.UUID | None = None
    line: str


class TriggeringChange(BaseModel):
    kind: ChangeKind
    # Commit SHA, secret ARN with version id, task definition ARN, Vercel deployment id.
    ref: str | None = None
    at: str | None = Field(default=None, description="ISO 8601 time of the change")
    description: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class Claim(BaseModel):
    text: str
    evidence_ids: list[str] = Field(description="Ids of the evidence lines this claim rests on")


class FixOption(BaseModel):
    """A fix the model suggests. Medic turns these into concrete, approvable fixes; it never
    runs one as written by the model."""

    kind: FixOptionKind
    description: str
    # Resource the fix acts on: service name, secret name, repository path.
    target: str | None = None


class RootCause(BaseModel):
    summary: str = Field(description="One or two sentences a person reads first")
    root_cause: str = Field(description="What broke and why")
    # The facts behind root_cause, each citing evidence ids.
    claims: list[Claim] = Field(default_factory=list)
    triggering_change: TriggeringChange | None = None
    affected_resources: list[str] = Field(default_factory=list, description="Resource names")
    confidence: Confidence
    fix_options: list[FixOption] = Field(default_factory=list)
    false_alarm: bool = Field(
        default=False,
        description=(
            "True only when nothing is wrong for users: error rate, HTTP status, and latency "
            "are normal in the evidence"
        ),
    )
    search_query: str | None = Field(
        default=None,
        description="A short web search for the root cause's mechanism, no names or ids",
        examples=["postgres missing index full table scan"],
    )
