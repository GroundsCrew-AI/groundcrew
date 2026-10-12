"""Dev-only self-approve for live test scripts: refused unless APP_ENV=dev and
LIVE_SCRIPT_SELF_APPROVE=1, and recorded as script:<name> (dev)."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.models import Approval, ApprovalStatus, Job, Run, RunStatus, RunStep, ToolPolicy
from app.db.session import SessionFactory, get_session_factory
from app.main import app
from app.policy.engine import PolicyViolationError
from app.policy.self_approve import self_approve

from .test_api import Member, _member, _settings

pytestmark = pytest.mark.anyio


@pytest.fixture
async def client(session_factory: SessionFactory) -> AsyncIterator[httpx.AsyncClient]:
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def alice(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 1001, "alice")


@pytest.fixture
async def bob(session_factory: SessionFactory) -> Member:
    return await _member(session_factory, 2002, "bob")


async def _waiting(sf: SessionFactory, workspace_id: uuid.UUID) -> uuid.UUID:
    async with sf() as session:
        run = Run(
            workspace_id=workspace_id, agent="deploy", goal="g", status=RunStatus.waiting_approval
        )
        session.add(run)
        await session.flush()
        approval = Approval(
            run_id=run.id,
            workspace_id=workspace_id,
            tool_call_id="deploy-plan",
            tool_name="target_apply",
            policy=ToolPolicy.write,
            arguments={"plan_hash": "a" * 64},
        )
        session.add(approval)
        await session.commit()
        return approval.id


def _use(**overrides: Any) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"app_env": "dev", "live_script_self_approve": False},
        {"app_env": "production", "live_script_self_approve": True},
        {"app_env": "staging", "live_script_self_approve": True},
    ],
)
async def test_refused_unless_dev_and_flag(
    client: httpx.AsyncClient,
    alice: Member,
    session_factory: SessionFactory,
    overrides: dict[str, Any],
) -> None:
    approval_id = await _waiting(session_factory, alice.workspace_id)
    _use(**overrides)
    r = await client.post(
        f"/approvals/{approval_id}/self-approve",
        json={"script": "live-phase4"},
        headers=alice.headers,
    )
    assert r.status_code == 404
    async with session_factory() as session:
        stored = await session.get(Approval, approval_id)
        assert stored is not None and stored.status == ApprovalStatus.pending


async def test_dev_with_flag_approves_as_the_script(
    client: httpx.AsyncClient, alice: Member, session_factory: SessionFactory
) -> None:
    approval_id = await _waiting(session_factory, alice.workspace_id)
    _use(app_env="dev", live_script_self_approve=True)
    r = await client.post(
        f"/approvals/{approval_id}/self-approve",
        json={"script": "live-phase4"},
        headers=alice.headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved" and body["decided_by"] is None
    assert body["decided_via"] == "script:live-phase4 (dev)"
    assert body["decided_by_name"] == "script:live-phase4 (dev)"
    async with session_factory() as session:
        step = await session.scalar(select(RunStep).where(RunStep.approval_id == approval_id))
        assert step is not None and step.output_summary == "approved by script:live-phase4 (dev)"
        jobs = (await session.scalars(select(Job).where(Job.kind == "resume_run"))).all()
        assert any(j.payload.get("approval_id") == str(approval_id) for j in jobs)


async def test_not_for_other_workspaces_or_bad_names(
    client: httpx.AsyncClient, alice: Member, bob: Member, session_factory: SessionFactory
) -> None:
    approval_id = await _waiting(session_factory, alice.workspace_id)
    _use(app_env="dev", live_script_self_approve=True)
    r = await client.post(
        f"/approvals/{approval_id}/self-approve",
        json={"script": "live-phase4"},
        headers=bob.headers,
    )
    assert r.status_code == 404
    r = await client.post(
        f"/approvals/{approval_id}/self-approve",
        json={"script": "Not A Name"},
        headers=alice.headers,
    )
    assert r.status_code == 422


async def test_the_function_checks_the_guard_too(
    session_factory: SessionFactory, alice: Member
) -> None:
    approval_id = await _waiting(session_factory, alice.workspace_id)
    async with session_factory() as session:
        with pytest.raises(PolicyViolationError, match="APP_ENV=dev"):
            await self_approve(
                session,
                approval_id,
                script="x",
                settings=_settings(app_env="production", live_script_self_approve=True),
            )
