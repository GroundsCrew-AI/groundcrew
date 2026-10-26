"""The Example workspace: a read-only copy of chosen work from a real workspace, for people who
have not connected anything yet (Workspace.is_example).

`build` copies the source workspace's connections (no secrets), its discovered resources and
edges, the incidents and deploys named (with their runs, steps, approvals, and fix PR
follow-ups), and its latest discovery run, into the example workspace, replacing what was
there. Every value is scrubbed on the way: AWS account ids, IP addresses, email addresses,
External IDs, the source's people, and anything that looks secret get stable placeholders,
and every id is new. `leaks` checks the copy for what must not survive; `build` refuses to
commit when it finds any.

    python -m app.admin_cli example candidates <source workspace id>
    python -m app.admin_cli example build <source workspace id> \\
        [--incident ID ...] [--deploy ID ...] [--apply]
"""

import ipaddress
import json
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Approval,
    Base,
    Connection,
    Edge,
    Event,
    Incident,
    IncidentFollowUp,
    Resource,
    Run,
    RunStatus,
    RunStep,
    User,
    Workspace,
    WorkspaceMember,
)
from app.workspace_settings import CONFIG_KEY, NotificationConfig, WorkspaceConfig

NAME = "Example workspace"
ACCOUNT = "123456789012"
EXTERNAL_ID = "example-external-id"
PERSON = "a team member"
PERSON_LOGIN = "example-user"
REMOVED = "[removed]"
DISCOVERY_AGENT = "discovery"

_ACCOUNT = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")
_IPV4 = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])")
_HOST_IP = re.compile(r"\bip-([0-9]{1,3})-([0-9]{1,3})-([0-9]{1,3})-([0-9]{1,3})\b")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# Values that are secrets whatever their key: Fernet tokens, Groundcrew and GitHub tokens,
# AWS access keys, PEM blocks.
_SECRET_VALUE = re.compile(
    r"gAAAAA[A-Za-z0-9_\-=]{20,}|gct_[A-Za-z0-9_\-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16}|-----BEGIN [A-Z ]+-----"
)
# String values under these keys are never copied.
_SECRET_KEY = re.compile(
    r"secret|password|passwd|token|private_key|api_key|apikey|credential|encrypted|webhook",
    re.IGNORECASE,
)
_KEEP_IPS = frozenset({"127.0.0.1", "0.0.0.0"})  # noqa: S104 - text, not a bind address
_PLACEHOLDER_EMAIL = re.compile(r"@example\.(?:com|org|net)$", re.IGNORECASE)


@dataclass
class Scrubber:
    """Replaces what identifies the source. The same input always gets the same placeholder,
    so a page that names one address twice still reads naturally."""

    ids: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    # The source's people and External IDs: replaced wherever they appear.
    words: dict[str, str] = field(default_factory=dict)
    ips: dict[str, str] = field(default_factory=dict)
    emails: dict[str, str] = field(default_factory=dict)

    def new_id(self, old: uuid.UUID) -> uuid.UUID:
        if old not in self.ids:
            self.ids[old] = uuid.uuid4()
        return self.ids[old]

    def _ip(self, ip: str) -> str:
        if ip in _KEEP_IPS:
            return ip
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            return ip  # a version number, not an address
        if ip not in self.ips:
            self.ips[ip] = f"203.0.113.{len(self.ips) % 250 + 1}"
        return self.ips[ip]

    def _email(self, email: str) -> str:
        if _PLACEHOLDER_EMAIL.search(email):
            return email
        if email not in self.emails:
            self.emails[email] = f"person{len(self.emails) + 1}@example.com"
        return self.emails[email]

    def text(self, value: str) -> str:
        for word, placeholder in self.words.items():
            value = value.replace(word, placeholder)
        value = _SECRET_VALUE.sub(REMOVED, value)
        value = _EMAIL.sub(lambda m: self._email(m.group(0)), value)
        value = _HOST_IP.sub(
            lambda m: "ip-" + self._ip(".".join(m.groups())).replace(".", "-"), value
        )
        value = _IPV4.sub(lambda m: self._ip(m.group(0)), value)
        value = _ACCOUNT.sub(ACCOUNT, value)
        return _UUID.sub(lambda m: str(self.new_id(uuid.UUID(m.group(0)))), value)

    def value(self, value: Any, key: str = "") -> Any:
        if isinstance(value, str):
            if key and _SECRET_KEY.search(key) and value:
                return REMOVED
            if key.lower() in ("external_id", "externalid"):
                return EXTERNAL_ID
            return self.text(value)
        if isinstance(value, uuid.UUID):
            return self.new_id(value)
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if k.lower() in ("installation_id", "installation"):
                    continue  # a GitHub App installation: never tracked from the copy
                out[self.text(k) if _UUID.search(k) else k] = self.value(v, k)
            return out
        if isinstance(value, list):
            return [self.value(v, key) for v in value]
        return value


@dataclass
class Plan:
    """What `build` copies, and how many rows of each."""

    incidents: list[uuid.UUID]
    deploys: list[uuid.UUID]
    counts: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


# Columns never copied: they hold secrets, or would make the copy act (embeddings feed
# incident memory; a run's messages are the raw model conversation).
_DROP: dict[type[Base], dict[str, Any]] = {
    Connection: {"secret_encrypted": None},
    Incident: {"embedding": None, "remembered_at": None},
    Run: {"messages": []},
    IncidentFollowUp: {"installation_id": None, "checked_at": None},
    Approval: {"decided_by": None, "decided_via": PERSON},
}
# Foreign keys to rows that are not copied: cleared.
_CLEAR = {"dismissed_by"}


def _copy(row: Base, scrub: Scrubber, example_id: uuid.UUID) -> dict[str, Any]:
    table = type(row)
    out: dict[str, Any] = {}
    for column in row.__table__.columns:
        name = column.key
        value = getattr(row, name)
        if name == "workspace_id":
            value = example_id
        elif name in _DROP.get(table, {}):
            value = _DROP[table][name]
        elif name in _CLEAR:
            value = None
        elif name in ("created_at", "updated_at") or value is None:
            pass
        else:
            value = scrub.value(value)  # key rules are for JSON keys; columns are listed above
        out[name] = value
    return out


async def _source_people(session: AsyncSession, workspace_id: uuid.UUID) -> dict[str, str]:
    """The source workspace's people: names, GitHub logins, and email addresses."""
    users = await session.scalars(
        select(User)
        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .where(WorkspaceMember.workspace_id == workspace_id)
    )
    words: dict[str, str] = {}
    for n, user in enumerate(users, 1):
        if user.email:
            words[user.email] = f"person{n}@example.com"
        if user.github_login and len(user.github_login) > 2:
            words[user.github_login] = PERSON_LOGIN
        if user.name and len(user.name) > 2:
            words[user.name] = PERSON
    # Longest first, so "Ann Lee" goes before "Ann".
    return dict(sorted(words.items(), key=lambda kv: -len(kv[0])))


async def example_workspace(session: AsyncSession) -> Workspace | None:
    found: Workspace | None = await session.scalar(
        select(Workspace).where(Workspace.is_example.is_(True))
    )
    return found


async def candidates(session: AsyncSession, source_id: uuid.UUID) -> str:
    """Resolved incidents and finished deploys of the source, to choose from."""
    incidents = await session.scalars(
        select(Incident)
        .where(Incident.workspace_id == source_id, Incident.status == "resolved")
        .order_by(Incident.created_at.desc())
        .limit(30)
    )
    deploys = await session.scalars(
        select(Run)
        .where(
            Run.workspace_id == source_id,
            Run.agent == "deploy",
            Run.status == RunStatus.succeeded,
        )
        .order_by(Run.created_at.desc())
        .limit(30)
    )
    lines = ["Resolved incidents (--incident):"]
    lines += [f"  {i.id}  {i.created_at:%Y-%m-%d}  {i.title[:90]}" for i in incidents]
    lines.append("Succeeded deploys (--deploy):")
    lines += [f"  {r.id}  {r.created_at:%Y-%m-%d}  {r.goal[:90]}" for r in deploys]
    return "\n".join(lines)


async def _remembered(session: AsyncSession, incidents: Iterable[Incident]) -> set[uuid.UUID]:
    """Past incidents the chosen ones reused through memory, so "Seen before" links work."""
    out: set[uuid.UUID] = set()
    for incident in incidents:
        memory = (incident.state or {}).get("memory") or {}
        if memory.get("incident_id"):
            out.add(uuid.UUID(str(memory["incident_id"])))
    return out


async def _clear(session: AsyncSession, example_id: uuid.UUID) -> None:
    """Remove the previous copy (the workspace row stays, so its id and links do too)."""
    runs = select(Run.id).where(Run.workspace_id == example_id)
    await session.execute(delete(RunStep).where(RunStep.run_id.in_(runs)))
    for model in (Approval, Run, IncidentFollowUp, Event, Incident, Edge, Resource, Connection):
        await session.execute(delete(model).where(model.workspace_id == example_id))


async def build(
    session: AsyncSession,
    source_id: uuid.UUID,
    incident_ids: list[uuid.UUID],
    deploy_ids: list[uuid.UUID],
) -> tuple[Workspace, Plan]:
    """Copy into the example workspace (made on first use). The caller commits, or rolls back
    for a dry run; nothing should be committed while `Plan.problems` is not empty."""
    source = await session.get(Workspace, source_id)
    if source is None or source.is_example:
        raise ValueError(f"No source workspace {source_id}.")
    plan = Plan(incidents=list(incident_ids), deploys=list(deploy_ids))
    scrub = Scrubber(words=await _source_people(session, source_id))

    connections = list(
        await session.scalars(select(Connection).where(Connection.workspace_id == source_id))
    )
    for conn in connections:
        external = (conn.config or {}).get("external_id")
        if isinstance(external, str) and external:
            scrub.words[external] = EXTERNAL_ID

    example = await example_workspace(session)
    if example is None:
        example = Workspace(name=NAME, owner_id=source.owner_id, is_example=True)
        session.add(example)
        await session.flush()
    else:
        await _clear(session, example.id)
    example.name = NAME
    summary = (source.settings or {}).get("system_summary")
    # Nothing watches or notifies from the copy.
    quiet = WorkspaceConfig(
        incident_watch=False,
        notifications=NotificationConfig(
            email="none",
            incident_opened=False,
            approval_needed=False,
            incident_resolved=False,
            incident_failed=False,
        ),
    )
    example.settings = {
        CONFIG_KEY: quiet.model_dump(),
        **({"system_summary": scrub.value(summary)} if summary else {}),
    }

    incidents = list(
        await session.scalars(
            select(Incident).where(
                Incident.workspace_id == source_id, Incident.id.in_(incident_ids)
            )
        )
    )
    missing = set(incident_ids) - {i.id for i in incidents}
    plan.problems += [f"incident {m} is not in the source workspace" for m in missing]
    extra = await _remembered(session, incidents) - {i.id for i in incidents}
    if extra:
        incidents += list(
            await session.scalars(
                select(Incident).where(Incident.workspace_id == source_id, Incident.id.in_(extra))
            )
        )
    chosen_incidents = [i.id for i in incidents]
    deploy_runs = list(
        await session.scalars(
            select(Run).where(
                Run.workspace_id == source_id, Run.id.in_(deploy_ids), Run.agent == "deploy"
            )
        )
    )
    plan.problems += [
        f"deploy {m} is not in the source workspace"
        for m in set(deploy_ids) - {r.id for r in deploy_runs}
    ]
    discovery = await session.scalar(
        select(Run)
        .where(
            Run.workspace_id == source_id,
            Run.agent == DISCOVERY_AGENT,
            Run.status == RunStatus.succeeded,
        )
        .order_by(Run.created_at.desc())
        .limit(1)
    )
    runs = deploy_runs + ([discovery] if discovery else [])
    if chosen_incidents:
        runs += list(
            await session.scalars(
                select(Run).where(
                    Run.workspace_id == source_id, Run.incident_id.in_(chosen_incidents)
                )
            )
        )
    run_ids = [r.id for r in runs]

    resources = list(
        await session.scalars(
            select(Resource).where(Resource.workspace_id == source_id, Resource.stale.is_(False))
        )
    )
    resource_ids = {r.id for r in resources}
    edges = [
        e
        for e in await session.scalars(select(Edge).where(Edge.workspace_id == source_id))
        if e.from_id in resource_ids and e.to_id in resource_ids
    ]
    approvals = (
        list(await session.scalars(select(Approval).where(Approval.run_id.in_(run_ids))))
        if run_ids
        else []
    )
    steps = (
        list(await session.scalars(select(RunStep).where(RunStep.run_id.in_(run_ids))))
        if run_ids
        else []
    )
    followups = (
        list(
            await session.scalars(
                select(IncidentFollowUp).where(IncidentFollowUp.incident_id.in_(chosen_incidents))
            )
        )
        if chosen_incidents
        else []
    )
    events = (
        list(await session.scalars(select(Event).where(Event.incident_id.in_(chosen_incidents))))
        if chosen_incidents
        else []
    )

    # Parents before children; run steps point at approvals, which point at runs.
    ordered: list[tuple[type[Base], list[Any]]] = [
        (Connection, connections),
        (Resource, resources),
        (Edge, edges),
        (Incident, incidents),
        (Run, runs),
        (Approval, approvals),
        (RunStep, steps),
        (IncidentFollowUp, followups),
        (Event, events),
    ]
    for model, rows in ordered:
        copied = [_copy(r, scrub, example.id) for r in rows]
        if model is Event:
            # An event on a resource that is gone (stale, not copied) keeps no link to it.
            for original, row in zip(rows, copied, strict=True):
                if original.resource_id not in resource_ids:
                    row["resource_id"] = None
        if copied:
            await session.execute(insert(model), copied)
        plan.counts[model.__tablename__] = len(rows)
    await session.flush()

    forbidden = _forbidden(source_id, connections, scrub)
    plan.problems += await leaks(session, example.id, forbidden)
    return example, plan


def _forbidden(source_id: uuid.UUID, connections: list[Connection], scrub: Scrubber) -> list[str]:
    values = [str(source_id), *(str(c.id) for c in connections)]
    values += [w for w, placeholder in scrub.words.items() if w != placeholder]
    values += [ip for ip in scrub.ips]
    values += [e for e in scrub.emails]
    return [v for v in values if v]


async def example_text(session: AsyncSession, example_id: uuid.UUID) -> str:
    """Every value in the example workspace's rows, as one text (for `leaks`)."""
    parts: list[str] = []
    workspace = await session.get(Workspace, example_id)
    if workspace is not None:
        parts.append(json.dumps(workspace.settings, default=str))
    runs = select(Run.id).where(Run.workspace_id == example_id)
    for model in (Connection, Resource, Edge, Incident, Run, Approval, IncidentFollowUp, Event):
        for row in await session.scalars(select(model).where(model.workspace_id == example_id)):
            parts.append(_row_text(row))
    for step in await session.scalars(select(RunStep).where(RunStep.run_id.in_(runs))):
        parts.append(_row_text(step))
    return "\n".join(parts)


def _row_text(row: Base) -> str:
    values = {
        c.key: getattr(row, c.key) for c in row.__table__.columns if c.key not in ("embedding",)
    }
    return json.dumps(values, default=str)


async def leaks(session: AsyncSession, example_id: uuid.UUID, forbidden: list[str]) -> list[str]:
    """What must not be in the example workspace and is: the source's ids, people, addresses,
    and External IDs, and any AWS account id, IP address, email address, or secret-looking
    value that is not a placeholder."""
    text = await example_text(session, example_id)
    found = [f"source value {v[:4]}... is in the copy" for v in forbidden if v in text]
    accounts = {a for a in _ACCOUNT.findall(text) if a != ACCOUNT}
    found += [f"AWS account id {a[:4]}... is in the copy" for a in sorted(accounts)]
    ips = {
        ip
        for ip in _IPV4.findall(text)
        if ip not in _KEEP_IPS and not ip.startswith("203.0.113.") and _is_ip(ip)
    }
    found += [f"IP address {ip} is in the copy" for ip in sorted(ips)]
    emails = {e for e in _EMAIL.findall(text) if not _PLACEHOLDER_EMAIL.search(e)}
    found += [f"email address {e} is in the copy" for e in sorted(emails)]
    if _SECRET_VALUE.search(text):
        found.append("a secret-looking value is in the copy")
    return found


def _is_ip(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return True
