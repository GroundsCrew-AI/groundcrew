"""Secrets Manager reader: metadata only (DescribeSecret).

Last changed, last accessed, rotation, and which version ids hold AWSCURRENT / AWSPREVIOUS.
Never a value: GetSecretValue is not in the read-only allowlist and the role denies it, and this
module never asks for it. A change inside the window is also a `secret_change` marker.
"""

from datetime import datetime
from typing import Any

from app.agents.medic.events import ChangeMarker
from app.connectors.aws import AwsReader
from app.telemetry.evidence import ReaderOutput, line, parse_time

SOURCE = "secrets"


def _stages(versions: dict[str, list[str]]) -> str:
    named = []
    for vid, stages in sorted(versions.items()):
        for stage in stages:
            named.append(f"{stage}={vid[:8]}")
    return ", ".join(sorted(named)) or "none"


def describe(secret: dict[str, Any], since: datetime, until: datetime) -> ReaderOutput:
    name = str(secret.get("Name") or secret.get("ARN") or "?")
    out = ReaderOutput()
    versions = secret.get("VersionIdsToStages") or {}
    changed = parse_time(secret.get("LastChangedDate"))
    if changed is not None:
        inside = since <= changed <= until
        text = f"value last changed{' (inside the incident window)' if inside else ''}"
        text += f"; versions: {_stages(versions)}"
        out.lines.append(line(changed, SOURCE, name, text))
        if inside:
            current = next((v for v, s in versions.items() if "AWSCURRENT" in s), None)
            out.changes.append(
                ChangeMarker(
                    kind="secret_change",
                    ref=f"{secret.get('ARN', name)}:{current or '?'}",
                    at=changed,
                    label=f"{name} changed",
                )
            )
    accessed = parse_time(secret.get("LastAccessedDate"))
    if accessed is not None:
        out.lines.append(line(accessed, SOURCE, name, "last accessed (day resolution)"))
    rotation = "on" if secret.get("RotationEnabled") else "off"
    out.lines.append(
        line(until, SOURCE, name, f"now: versions {_stages(versions)}; rotation {rotation}")
    )
    return out


def read_secret(
    reader: AwsReader, region: str | None, secret_id: str, since: datetime, until: datetime
) -> ReaderOutput:
    """Blocking (boto3): call with asyncio.to_thread."""
    secret = reader.call("secretsmanager", region, "DescribeSecret", SecretId=secret_id)
    return describe(secret, since, until)
