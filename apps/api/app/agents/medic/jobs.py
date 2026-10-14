"""Worker handlers for Medic, and its resume registration (app.pipeline.resume)."""

import uuid
from typing import Any

from app.agents.medic import agent
from app.db.session import SessionFactory
from app.pipeline.resume import register_resume_handler
from app.security.redact import redact

VERIFY_JOB = agent.VERIFY_JOB
NO_FIX_JOB = agent.NO_FIX_JOB


def _runner(session_factory: SessionFactory) -> agent.MedicRunner:
    return agent.MedicRunner(session_factory, agent._deps_factory(session_factory))


async def investigate_incident_job(
    session_factory: SessionFactory, payload: dict[str, Any]
) -> None:
    run_id = uuid.UUID(payload["run_id"])
    try:
        runner = _runner(session_factory)
    except Exception as exc:  # usually Token Factory not configured
        await agent.fail_without_deps(
            session_factory,
            run_id,
            redact(f"Medic cannot start: {type(exc).__name__}: {exc}")[:500],
        )
        return
    await runner.investigate(uuid.UUID(payload["incident_id"]), run_id)


async def medic_resume_job(session_factory: SessionFactory, payload: dict[str, Any]) -> None:
    await _runner(session_factory).resume(uuid.UUID(payload["run_id"]))


async def verify_incident_job(session_factory: SessionFactory, payload: dict[str, Any]) -> None:
    await _runner(session_factory).verify(
        uuid.UUID(payload["incident_id"]), uuid.UUID(payload["run_id"]), payload.get("chain")
    )


async def rediagnose_incident_job(session_factory: SessionFactory, payload: dict[str, Any]) -> None:
    await _runner(session_factory).rediagnose(
        uuid.UUID(payload["incident_id"]),
        uuid.UUID(payload["run_id"]),
        [str(s) for s in payload.get("signals") or []],
    )


async def no_fix_deadline_job(session_factory: SessionFactory, payload: dict[str, Any]) -> None:
    await _runner(session_factory).no_fix_deadline(
        uuid.UUID(payload["incident_id"]), uuid.UUID(payload["run_id"])
    )


register_resume_handler(agent.AGENT, medic_resume_job)
