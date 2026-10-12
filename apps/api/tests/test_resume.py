"""resume_run picks a handler by run.agent (app.pipeline.resume), made for Phase 5."""

import uuid
from typing import Any

import pytest

from app.db.models import Run, RunStatus, Workspace
from app.db.session import SessionFactory
from app.pipeline import resume
from app.pipeline.runner import pipeline_job

pytestmark = pytest.mark.anyio


async def _run(sf: SessionFactory, ws: Workspace, agent: str) -> uuid.UUID:
    async with sf() as session:
        run = Run(workspace_id=ws.id, agent=agent, goal="g", status=RunStatus.queued)
        session.add(run)
        await session.commit()
        return run.id


def test_deploy_runs_resume_in_the_pipeline() -> None:
    assert resume.resume_handler("deploy") is pipeline_job


async def test_dispatch_by_agent(
    session_factory: SessionFactory, workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []

    def spy(name: str) -> Any:
        async def handler(_sf: SessionFactory, payload: dict[str, Any]) -> None:
            calls.append((name, payload["run_id"]))

        return handler

    monkeypatch.setitem(resume._handlers, "deploy", spy("pipeline"))
    monkeypatch.setattr("app.agents.base.run_agent_job", spy("agent_runner"))
    resume.register_resume_handler("medic", spy("medic"))
    try:
        deploy = await _run(session_factory, workspace, "deploy")
        medic = await _run(session_factory, workspace, "medic")
        other = await _run(session_factory, workspace, "something_new")
        for run_id in (deploy, medic, other):
            await resume.resume_run_job(session_factory, {"run_id": str(run_id)})
    finally:
        resume._handlers.pop("medic", None)
    assert calls == [
        ("pipeline", str(deploy)),
        ("medic", str(medic)),
        ("agent_runner", str(other)),
    ]
