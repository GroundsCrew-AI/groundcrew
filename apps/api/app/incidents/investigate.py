"""Worker handler for "investigate_incident" jobs: the Medic run of an incident.

Until the Medic agent lands (Phase 5 M3) the run only marks the incident `investigating` and
finishes, saying Medic is not available yet. The incident stays open, so the watcher keeps
deduping new signals into it instead of opening a new incident every pass.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from app.db.models import Incident, Run, RunStatus, StopReason
from app.db.session import SessionFactory
from app.incidents.store import set_status


async def investigate_incident_job(
    session_factory: SessionFactory, payload: dict[str, Any]
) -> None:
    async with session_factory() as session:
        incident = await session.get(Incident, uuid.UUID(payload["incident_id"]))
        run = await session.get(Run, uuid.UUID(payload["run_id"]))
        if incident is None or run is None:
            return
        now = datetime.now(UTC)
        run.status = RunStatus.running
        run.started_at = run.started_at or now
        await set_status(session, incident, run, "investigating", now=now)
        run.status = RunStatus.stopped
        run.stop_reason = StopReason.completed
        run.result = "Incident recorded. The Medic agent is not available yet (Phase 5 M3)."
        run.finished_at = datetime.now(UTC)
        await session.commit()
