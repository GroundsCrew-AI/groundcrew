"""Medic: investigate an incident, propose fixes, apply them after approval, verify, remember.

Three entry points, each a worker job on the incident's Medic run:

- `investigate` ("investigate_incident"): gather -> memory -> research -> diagnose -> propose.
  Each stage records its typed event (docs/phase5-interfaces.md, section 7) and saves
  `incidents.state`; model calls are `llm` steps with model, tokens, and cost. Then one approval
  per fix, and the run waits. A hard timeout (MEDIC_TIMEOUT_SECONDS) bounds the investigation.
- `resume` ("resume_run" for agent "medic", app.pipeline.resume): apply approved fixes in order
  with single-use tokens, skip rejected ones, and wait again at the first undecided one. Once
  every fix is decided and one applied, verification starts.
- `verify` ("verify_incident"): re-check the signals every MEDIC_VERIFY_INTERVAL_SECONDS; two
  healthy rounds resolve the incident (and remember it), MEDIC_VERIFY_TIMEOUT_SECONDS fails it.

Caps: the run's USD and token caps and MAX_ULTRA_CALLS_PER_INCIDENT (RunBudget), Tavily calls
per incident (TAVILY_MAX_CALLS_PER_INCIDENT).
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import queue
from app.actions.base import EmergencyActionError, EmergencyActions
from app.agents.medic import diagnose as dx
from app.agents.medic import evidence as ev
from app.agents.medic import fixes as fx
from app.agents.medic import memory as mem
from app.agents.medic.events import (
    Diagnosis,
    EvidenceCollected,
    EvidenceSourceSummary,
    FixApplied,
    FixApproval,
    FixProposed,
    IncidentFailed,
    IncidentResolved,
    IncidentStatus,
    IncidentUpdated,
    MemoryChecked,
    ResearchDone,
    ResearchResult,
    Signal,
)
from app.agents.medic.state import FixRecord, MedicState, MemoryUsed, Outcome
from app.agents.medic.verify import k8s_checks, run_checks
from app.config import Settings, get_settings
from app.connectors.aws import AwsReader
from app.connectors.github import GitHubApp, get_github_app
from app.db.models import (
    TERMINAL_RUN_STATUSES,
    Approval,
    ApprovalStatus,
    Connection,
    Edge,
    Incident,
    Resource,
    Run,
    RunStatus,
    StopReason,
    ToolPolicy,
)
from app.db.session import SessionFactory
from app.incidents import followups as fu
from app.incidents.store import material_signals, set_status
from app.incidents.views import refresh_pull_requests
from app.incidents.watcher import KubeFor, default_kube_for, default_reader_for
from app.llm.budget import BudgetExceededError, PgUsageStore, RunBudget
from app.llm.client import TokenFactory
from app.llm.recorder import StepRecorder
from app.pipeline.events import VerifyCheck, VerifyResult
from app.policy.actions import OPEN_FIX_PR, issue_approved_action
from app.security.redact import redact
from app.steps import record_step
from app.telemetry.collect import Evidence, GraphEvidenceCollector
from app.tools.github_pr import FixPullRequests, GitHubFixPullRequests
from app.tools.tavily import (
    TavilyClient,
    TavilyError,
    dominant_error,
    relevant,
    search_query,
    stack_terms,
    with_stack,
)

logger = logging.getLogger("groundcrew.medic")

AGENT: Final = "medic"
VERIFY_JOB: Final = "verify_incident"
NO_FIX_JOB: Final = "medic_no_fix"
HEALTHY_ROUNDS = 2
MAX_REDIAGNOSES = 2
# Evidence sources that carry the application's own log lines.
APP_LOGS = ("cloudwatch_logs:", "vercel:", "k8s:")

Collect = Callable[[uuid.UUID, list[uuid.UUID], datetime, datetime], Awaitable[Evidence]]


@dataclass
class MedicDeps:
    settings: Settings
    llm: TokenFactory
    collect: Collect
    actions: EmergencyActions | None
    prs: FixPullRequests | None
    github: GitHubApp | None
    tavily: TavilyClient | None
    reader_for: Callable[[Connection], AwsReader]
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    # Kubernetes Deployments' recovery checks read the cluster through this.
    kube_for: KubeFor = default_kube_for


def _emergency_actions(
    session_factory: SessionFactory, settings: Settings
) -> EmergencyActions | None:
    """The Operate-tier actions (app.actions); each plan checks the connection's operate
    capability, so accounts without it get a note instead of an emergency action."""
    from app.actions import get_emergency_actions

    return get_emergency_actions(session_factory, settings)


def default_deps(session_factory: SessionFactory, settings: Settings | None = None) -> MedicDeps:
    settings = settings or get_settings()
    github = get_github_app()
    has_github = github.configured()
    tavily = None
    if settings.tavily_api_key is not None and settings.tavily_api_key.get_secret_value():
        tavily = TavilyClient(settings)
    return MedicDeps(
        settings=settings,
        llm=TokenFactory(settings),
        collect=GraphEvidenceCollector(session_factory, settings=settings).collect,
        actions=_emergency_actions(session_factory, settings),
        prs=GitHubFixPullRequests(session_factory, github) if has_github else None,
        github=github if has_github else None,
        tavily=tavily,
        reader_for=default_reader_for(settings),
    )


DepsFactory = Callable[[SessionFactory], MedicDeps]
_deps_factory: DepsFactory = default_deps


def set_deps_factory(factory: DepsFactory | None) -> None:
    """Tests swap in fakes; None restores the default."""
    global _deps_factory
    _deps_factory = factory or default_deps


def _signals(incident: Incident) -> list[Signal]:
    return [Signal.model_validate(s) for s in incident.signals]


def _fix_key(rec: FixRecord) -> tuple[object, ...]:
    """What a fix does, exactly: the action and the plan's hash (or the PR draft's), and what it
    waits for. Titles and wording do not count."""
    fix = rec.fix
    plan = fix.plan.plan_hash() if fix.plan else None
    draft = rec.draft.plan_hash() if rec.draft else None
    return (fix.kind, fix.action, plan, draft, len(fix.depends_on))


def _same_fixes(before: list[FixRecord], after: list[FixRecord]) -> bool:
    """A re-diagnosis that proposes the very same fixes (same plans, same order)."""
    return len(before) == len(after) and [_fix_key(r) for r in before] == [
        _fix_key(r) for r in after
    ]


def _round_ids(records: list[FixRecord], round_: int) -> list[FixRecord]:
    """After a re-diagnosis, new fix ids (fix-1-r1) so they never collide with the superseded
    fixes' approvals and timeline cards."""
    if round_ == 0:
        return records
    ids = {r.fix.fix_id: f"{r.fix.fix_id}-r{round_}" for r in records}
    for r in records:
        r.fix = r.fix.model_copy(
            update={
                "fix_id": ids[r.fix.fix_id],
                "depends_on": [ids.get(d, d) for d in r.fix.depends_on],
            }
        )
    return records


def _save(incident: Incident, state: MedicState) -> None:
    incident.state = state.model_dump(mode="json")


class MedicRunner:
    def __init__(self, session_factory: SessionFactory, deps: MedicDeps) -> None:
        self.session_factory = session_factory
        self.deps = deps
        self.settings = deps.settings

    # ------------------------------------------------------------------ helpers

    async def _load(
        self, session: AsyncSession, incident_id: uuid.UUID | None, run_id: uuid.UUID
    ) -> tuple[Incident, Run, MedicState] | None:
        run = await session.get(Run, run_id)
        if run is None:
            return None
        incident = await session.get(Incident, incident_id or run.incident_id)
        if incident is None:
            return None
        return incident, run, MedicState.model_validate(incident.state or {})

    async def _graph(self, session: AsyncSession, incident: Incident) -> fx.Graph:
        ws = incident.workspace_id
        resources = {
            r.id: r
            for r in await session.scalars(select(Resource).where(Resource.workspace_id == ws))
        }
        edges = list(await session.scalars(select(Edge).where(Edge.workspace_id == ws)))
        connections = {
            c.id: c
            for c in await session.scalars(select(Connection).where(Connection.workspace_id == ws))
        }
        return fx.Graph(resources, edges, connections, list(incident.affected_resource_ids))

    async def _status(
        self,
        session: AsyncSession,
        incident: Incident,
        run: Run,
        status: IncidentStatus,
        reason: str | None = None,
    ) -> None:
        await set_status(session, incident, run, status, reason=reason, now=self.deps.now())

    async def _finish(
        self,
        session: AsyncSession,
        run: Run,
        status: RunStatus,
        stop: StopReason,
        *,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        run.status, run.stop_reason = status, stop
        run.result, run.error = result, error
        run.finished_at = self.deps.now()

    async def _fail(
        self,
        session: AsyncSession,
        incident: Incident,
        run: Run,
        state: MedicState,
        reason: str,
        *,
        still_failing: list[str] | None = None,
        run_status: RunStatus = RunStatus.failed,
        stop: StopReason = StopReason.error,
    ) -> None:
        await self._close_superseded(session, state)
        state.phase, state.error = "done", reason
        state.outcome = Outcome(result="failed", reason=reason, still_failing=still_failing or [])
        _save(incident, state)
        await self._status(session, incident, run, "failed", reason)
        await record_step(
            session,
            run,
            kind="incident_failed",
            output_summary=reason,
            data=IncidentFailed(
                incident_id=incident.id,
                reason=reason,
                still_failing=still_failing or [],
                cost_usd=float(run.cost_usd) if run.cost_usd is not None else None,
            ),
        )
        await self._finish(
            session,
            run,
            run_status,
            stop,
            result=None if run_status == RunStatus.failed else reason,
            error=reason if run_status == RunStatus.failed else None,
        )
        await session.commit()

    # -------------------------------------------------------------- investigate

    async def investigate(self, incident_id: uuid.UUID, run_id: uuid.UUID) -> None:
        async with self.session_factory() as session:
            loaded = await self._load(session, incident_id, run_id)
            if loaded is None:
                return
            incident, run, state = loaded
            if run.status in TERMINAL_RUN_STATUSES:
                return
            run.status, run.stop_reason = RunStatus.running, None
            run.started_at = run.started_at or self.deps.now()
            state.run_id, state.phase = run.id, "investigating"
            await self._status(session, incident, run, "investigating")
            _save(incident, state)
            await session.commit()
            budget = await RunBudget.load(
                session, run, PgUsageStore(self.session_factory, run.workspace_id), self.settings
            )
            recorder = StepRecorder(session, run, AGENT)
            try:
                await asyncio.wait_for(
                    self._investigate(session, incident, run, state, budget, recorder),
                    self.settings.medic_timeout_seconds,
                )
            except BudgetExceededError as exc:
                await session.rollback()
                await self._fail(
                    session, incident, run, state, f"Budget reached: {exc}", stop=StopReason.budget
                )
            except TimeoutError:
                await session.rollback()
                await self._fail(
                    session,
                    incident,
                    run,
                    state,
                    f"Investigation took longer than {self.settings.medic_timeout_seconds} s.",
                )
            except Exception as exc:
                logger.exception(
                    "medic investigation failed", extra={"incident_id": str(incident.id)}
                )
                await session.rollback()
                await self._fail(
                    session, incident, run, state, redact(f"{type(exc).__name__}: {exc}")[:500]
                )

    async def _investigate(
        self,
        session: AsyncSession,
        incident: Incident,
        run: Run,
        state: MedicState,
        budget: RunBudget,
        recorder: StepRecorder,
    ) -> None:
        deps, llm = self.deps, self.deps.llm
        signals = _signals(incident)
        affected = set(incident.affected_resource_ids)
        now = deps.now()

        # 1. Gather
        evidence = await deps.collect(
            incident.workspace_id, list(incident.affected_resource_ids), incident.window_start, now
        )
        symptom = await mem.symptom_text(session, incident)
        all_items = ev.to_items(evidence)
        items, embedded = await ev.rank(all_items, symptom, affected, llm, budget)
        state.sources = [
            EvidenceSourceSummary(
                source=k, lines=len(v), truncated=evidence.truncated.get(k, False)
            )
            for k, v in sorted(evidence.sources.items())
        ]
        state.neighbors, state.evidence, state.changes = evidence.neighbors, items, evidence.changes
        _save(incident, state)
        await record_step(
            session,
            run,
            kind="evidence_collected",
            output_summary=(
                f"{len(items)} of {sum(len(v) for v in evidence.sources.values())} lines from "
                f"{len(evidence.sources)} sources, ranked by "
                f"{'embeddings' if embedded else 'rules'}"
            ),
            data=EvidenceCollected(
                sources=state.sources,
                neighbors=evidence.neighbors,
                items=items,
                changes=evidence.changes,
                total_lines=sum(len(v) for v in evidence.sources.values()),
            ),
        )
        await session.commit()

        # 2. Memory
        vector = await mem.embed_symptom(llm, budget, symptom)
        if vector is not None and not incident.is_test:
            incident.embedding = vector
        hits = await mem.similar(session, incident, vector, evidence.changes)
        reused = mem.reusable(hits, self.settings.medic_memory_threshold)
        state.memory_hits = hits
        if reused is not None:
            state.memory = MemoryUsed(
                incident_id=reused.incident_id,
                title=reused.title,
                similarity=reused.similarity,
                reused=["diagnosis", "fix"],
            )
        _save(incident, state)
        await record_step(
            session,
            run,
            kind="memory_checked",
            output_summary=(
                f"Reusing {reused.title} ({reused.similarity:.2f})"
                if reused
                else f"{len(hits)} similar past incidents, none close enough to reuse"
                + (
                    f" (best {hits[0].similarity:.2f}, needs "
                    f"{self.settings.medic_memory_threshold:.2f})"
                    if hits
                    else ""
                )
            ),
            data=MemoryChecked(
                hits=hits,
                reused_incident_id=reused.incident_id if reused else None,
                reused=state.memory.reused if state.memory else [],
            ),
        )
        await session.commit()

        # 3. Research. Queries carry the application's stack, from its lines and the graph (an
        # RDS engine), so a Postgres app's error does not bring back SQL Server articles.
        graph = await self._graph(session, incident)
        stack_lines = [
            *(i.line for i in all_items),
            *(f"{r.type} {r.config.get('engine') or ''}" for r in graph.resources.values()),
        ]
        state.stack = stack_terms(stack_lines)
        results, queries, skipped = await self._research(
            items, all_items, affected, state, reused is not None
        )
        state.research, state.research_queries = results, queries
        _save(incident, state)
        await record_step(
            session,
            run,
            kind="research_done",
            output_summary=skipped or f"{len(results)} results for {queries[0]!r}",
            data=ResearchDone(queries=queries, results=results, skipped_reason=skipped),
        )
        await session.commit()

        # 4. Diagnose
        names = [r.name for r in state.neighbors if r.id in affected] or [
            str(s.resource_name) for s in signals if s.resource_name
        ]
        affected_names = [
            graph.resources[r].name for r in incident.affected_resource_ids if r in graph.resources
        ] or names
        prompt = dx.messages(
            incident.title, signals, affected_names, items, evidence.changes, results, reused
        )
        raw = await dx.diagnose(llm, budget, prompt, confirm=reused is not None, on_call=recorder)
        root_cause, dropped = dx.check(raw, items, evidence.changes)
        live: VerifyResult | None = None
        if root_cause.false_alarm:
            # "False alarm" must survive a look at error rate and latency right now.
            live = await self._live_checks(
                session, incident, graph, state, since=incident.created_at
            )
            if live is None or not live.ok:
                failing = [c.detail for c in live.checks if not c.ok] if live else ["no AWS access"]
                dropped.append("false alarm rejected: " + "; ".join(failing)[:400])
                retry = [
                    *prompt,
                    {"role": "assistant", "content": dx.summary_json(root_cause)},
                    {
                        "role": "user",
                        "content": dx.not_false_alarm(failing),
                    },
                ]
                # Only the false-alarm call is in question: confirm on Super, not a new Ultra run.
                raw = await dx.diagnose(llm, budget, retry, confirm=True, on_call=recorder)
                root_cause, more = dx.check(raw, items, evidence.changes)
                dropped += more
                if root_cause.false_alarm:
                    root_cause = root_cause.model_copy(
                        update={"false_alarm": False, "confidence": "low"}
                    )
        state.root_cause, state.dropped = root_cause, dropped
        incident.root_cause = root_cause.root_cause
        onset = dx.change_at(root_cause, evidence.changes)
        if (
            onset is not None
            and incident.first_signal_at is not None
            and incident.first_signal_at < onset <= (incident.created_at or now)
        ):
            # The break cannot start before the change that caused it.
            incident.first_signal_at = onset
        _save(incident, state)
        await record_step(
            session,
            run,
            kind="diagnosis",
            output_summary=root_cause.summary
            + (f" ({len(dropped)} uncited items dropped)" if dropped else ""),
            data=Diagnosis(root_cause=root_cause, dropped=dropped),
        )
        await session.commit()

        # New evidence while diagnosing (slow queries, a bigger spike): diagnose again with it.
        await session.refresh(incident, ["signals"])
        material = material_signals(
            [x.model_dump(mode="json") for x in signals],
            incident.signals[len(signals) :],
        )
        if material and state.rediagnosed < MAX_REDIAGNOSES:
            state.rediagnosed += 1
            _save(incident, state)
            await self._note_rediagnosis(
                session, incident, run, [f"{m['rule']}: {m['detail']}" for m in material]
            )
            await session.commit()
            await self._investigate(session, incident, run, state, budget, recorder)
            return

        if root_cause.false_alarm and live is not None and live.ok:
            await self._resolve(
                session,
                incident,
                run,
                state,
                reason="False alarm: error rate, health, and latency are normal now.",
            )
            return

        # 4b. Research the root cause itself (an index, a pool setting), for the fix.
        follow_up: list[ResearchResult] = []
        if reused is None and root_cause.search_query:
            # Cleaned like every other query (no ids, times, raw JSON), with the stack (the
            # diagnosis may name the libraries the lines did not: "sqlalchemy engine.begin").
            asked = search_query(root_cause.search_query) or root_cause.search_query
            state.stack = stack_terms(
                [*stack_lines, root_cause.summary, root_cause.root_cause, asked]
            )
            follow_up, query, skipped = await self._search(asked, state)
            if query:
                state.research = [*state.research, *follow_up]
                state.research_queries = [*state.research_queries, query]
            _save(incident, state)
            await record_step(
                session,
                run,
                kind="research_done",
                output_summary=skipped or f"{len(follow_up)} results for {query!r}",
                data=ResearchDone(
                    queries=[query] if query else [], results=follow_up, skipped_reason=skipped
                ),
            )
            await session.commit()

        # 5. Propose
        emergency = await fx.emergency(
            root_cause, signals, graph, evidence.changes, deps.actions, memory=reused
        )
        pr = await fx.pull_request(
            incident.id,
            root_cause,
            signals,
            graph,
            evidence.changes,
            deps.github,
            llm,
            budget,
            order=len(emergency.records) + 1,
            on_call=recorder,
            research=follow_up,
        )
        proposed = emergency.records + pr.records
        if state.superseded and _same_fixes(state.superseded, proposed):
            await self._keep_superseded(session, incident, run, state, evidence.notes)
            return
        await self._close_superseded(session, state)
        state.fixes = _round_ids(proposed, state.rediagnosed)
        state.notes = [*evidence.notes, *emergency.notes, *pr.notes]
        _save(incident, state)
        await record_step(
            session,
            run,
            kind="fix_proposed",
            output_summary="; ".join([f.fix.title for f in state.fixes] + state.notes) or "No fix",
            data=FixProposed(fixes=[f.fix for f in state.fixes]),
        )
        if not state.fixes:
            grace = self.settings.medic_no_fix_grace_seconds
            if not state.no_fix_waited and grace > 0 and not root_cause.false_alarm:
                # The same outage may show up on a related service in a moment (a worker's
                # errors, then the api's): wait for it to join before giving up.
                now = self.deps.now()
                state.no_fix_until, state.no_fix_waited = now + timedelta(seconds=grace), True
                _save(incident, state)
                await record_step(
                    session,
                    run,
                    kind="incident_updated",
                    output_summary=f"No fix yet; waiting {grace} s for related signals",
                    data=IncidentUpdated(
                        incident_id=incident.id,
                        status=incident.status,
                        previous_status=incident.status,
                        at=now,
                        reason=f"No fix yet; waiting {grace} s for related signals.",
                    ),
                )
                await queue.enqueue(
                    session,
                    NO_FIX_JOB,
                    {"incident_id": str(incident.id), "run_id": str(run.id)},
                    run_after=state.no_fix_until,
                    max_attempts=1,
                )
                await session.commit()
                return
            await self._fail_no_fix(session, incident, run, state)
            return
        await self._request_approvals(session, incident, run, state)

    async def _keep_superseded(
        self,
        session: AsyncSession,
        incident: Incident,
        run: Run,
        state: MedicState,
        notes: list[str],
    ) -> None:
        """The re-diagnosis proposes exactly the fixes already waiting for approval: keep them and
        their approvals (no new cards to approve again). A decision made meanwhile is applied."""
        state.fixes, state.superseded = state.superseded, []
        state.notes = notes
        state.phase = "waiting_approval"
        _save(incident, state)
        await record_step(
            session,
            run,
            kind="fix_proposed",
            output_summary="Same fixes as before the new signals; their approvals stay: "
            + "; ".join(f.fix.title for f in state.fixes),
            data=FixProposed(fixes=[f.fix for f in state.fixes]),
        )
        await self._status(session, incident, run, "waiting_approval")
        decided = False
        for rec in state.fixes:
            approval = (
                await session.get(Approval, rec.approval_id, populate_existing=True)
                if rec.approval_id
                else None
            )
            decided = decided or (
                approval is not None and approval.status != ApprovalStatus.pending
            )
        if decided:
            # Approved or rejected while Medic diagnosed again: go on as decide_approval would.
            run.status, run.stop_reason = RunStatus.queued, None
            await queue.enqueue(session, "resume_run", {"run_id": str(run.id)})
        else:
            run.status, run.stop_reason = RunStatus.waiting_approval, StopReason.waiting_approval
        await session.commit()

    async def _close_superseded(self, session: AsyncSession, state: MedicState) -> None:
        """Close the approvals of fixes a re-diagnosis did not propose again."""
        now = self.deps.now()
        for rec in state.superseded:
            approval = (
                await session.get(
                    Approval, rec.approval_id, with_for_update=True, populate_existing=True
                )
                if rec.approval_id
                else None
            )
            if approval is not None and approval.status == ApprovalStatus.pending:
                approval.status = ApprovalStatus.rejected
                approval.decided_at = now
                approval.decided_via = "Medic: superseded by new signals"
        state.superseded = []

    async def _fail_no_fix(
        self, session: AsyncSession, incident: Incident, run: Run, state: MedicState
    ) -> None:
        state.no_fix_until = None
        await self._fail(
            session,
            incident,
            run,
            state,
            "No fix Medic can apply. " + " ".join(state.notes),
            run_status=RunStatus.succeeded,
            stop=StopReason.completed,
        )

    async def no_fix_deadline(self, incident_id: uuid.UUID, run_id: uuid.UUID) -> None:
        """The grace after "No fix" is over and no related signal re-diagnosed it: fail."""
        async with self.session_factory() as session:
            loaded = await self._load(session, incident_id, run_id)
            if loaded is None:
                return
            incident, run, state = loaded
            await session.get(Run, run.id, with_for_update=True, populate_existing=True)
            if (
                run.status in TERMINAL_RUN_STATUSES
                or state.phase != "investigating"
                or state.no_fix_until is None
                or state.fixes
            ):
                return
            await self._fail_no_fix(session, incident, run, state)

    async def _research(
        self,
        items: list[Any],
        all_items: list[Any],
        affected: set[uuid.UUID],
        state: MedicState,
        reused: bool,
    ) -> tuple[list[ResearchResult], list[str], str | None]:
        if reused:
            return [], [], "Skipped: a similar past incident was reused."
        # The application's most frequent error, not the loudest symptom (a failed health
        # check says nothing a search can use); the best-scored error line otherwise.
        query = dominant_error(i.line for i in all_items if i.source.startswith(APP_LOGS))
        if not query:
            # No application error: the dominant error from any source (an OOM stop reason),
            # never a raw line or the loudest non-error symptom.
            query = dominant_error(i.line for i in all_items)
        if not query:
            scored = sorted(items, key=lambda i: -ev.score(i, affected))
            query = next((q for i in scored if (q := search_query(i.line))), "")
        if not query:
            return [], [], "Skipped: no error line to search for."
        results, used, skipped = await self._search(query, state)
        return results, [used] if used else [], skipped

    async def _search(
        self, query: str, state: MedicState
    ) -> tuple[list[ResearchResult], str | None, str | None]:
        """One Tavily call within TAVILY_MAX_CALLS_PER_INCIDENT: results, the query sent (None
        when no call was made), and why it was skipped or failed."""
        if self.deps.tavily is None:
            return [], None, "Skipped: TAVILY_API_KEY is not set."
        if state.tavily_calls >= self.settings.tavily_max_calls_per_incident:
            return [], None, "Skipped: research cap for this incident reached."
        # The application's stack from its own lines ("postgres asyncpg sqlalchemy"), so a
        # Postgres app's connection error does not bring back SQL Server articles.
        stack = state.stack
        query = with_stack(query, stack)
        state.tavily_calls += 1
        try:
            found = await self.deps.tavily.search(query)
        except TavilyError as exc:
            return [], query, f"Research failed: {exc}"
        kept = relevant(found, query, stack)
        dropped = len(found) - len(kept)
        return (
            [ResearchResult(title=r.title, url=r.url, snippet=r.snippet) for r in kept],
            query,
            f"Dropped {dropped} unrelated results." if dropped and not kept else None,
        )

    async def _request_approvals(
        self, session: AsyncSession, incident: Incident, run: Run, state: MedicState
    ) -> None:
        for rec in state.fixes:
            tool = rec.fix.action or OPEN_FIX_PR
            plan_hash = (
                rec.fix.plan.plan_hash()
                if rec.fix.plan
                else rec.draft.plan_hash()
                if rec.draft
                else ""
            )
            approval = Approval(
                run_id=run.id,
                workspace_id=incident.workspace_id,
                tool_call_id=f"medic:{incident.id}:{rec.fix.fix_id}",
                tool_name=tool,
                policy=ToolPolicy.write if tool == OPEN_FIX_PR else ToolPolicy.emergency,
                arguments={
                    "plan_hash": plan_hash,
                    "fix_id": rec.fix.fix_id,
                    "summary": rec.fix.title,
                },
                status=ApprovalStatus.pending,
            )
            session.add(approval)
            await session.flush()
            rec.approval_id = approval.id
            rec.fix = rec.fix.model_copy(update={"approval_id": approval.id})
            rec.status = "waiting_approval"
            await record_step(
                session,
                run,
                kind="approval_requested",
                tool_name=tool,
                approval_id=approval.id,
                output_summary=f"Waiting for approval of {rec.fix.title}",
                data=FixApproval(incident_id=incident.id, fix=rec.fix),
            )
        state.phase = "waiting_approval"
        _save(incident, state)
        await self._status(session, incident, run, "waiting_approval")
        run.status, run.stop_reason = RunStatus.waiting_approval, StopReason.waiting_approval
        await session.commit()

    # ------------------------------------------------------------------- resume

    async def resume(self, run_id: uuid.UUID) -> None:
        async with self.session_factory() as session:
            loaded = await self._load(session, None, run_id)
            if loaded is None:
                return
            incident, run, state = loaded
            if run.status in TERMINAL_RUN_STATUSES or state.phase not in (
                "waiting_approval",
                "applying",
            ):
                return
            run.status, run.stop_reason = RunStatus.running, None
            await session.commit()
            while True:
                pending = await self._apply_decided(session, incident, run, state)
                if pending is None:
                    break
                # Lock the run, then look again: decide_approval locks it too, so a decision
                # either lands before this read or sees the run waiting and enqueues a resume.
                await session.get(Run, run.id, with_for_update=True, populate_existing=True)
                approval = await session.get(Approval, pending, populate_existing=True)
                if approval is not None and approval.status == ApprovalStatus.pending:
                    state.phase = "waiting_approval"
                    _save(incident, state)
                    await self._status(session, incident, run, "waiting_approval")
                    run.status, run.stop_reason = (
                        RunStatus.waiting_approval,
                        StopReason.waiting_approval,
                    )
                    await session.commit()
                    return
                await session.commit()
            await self._after_decisions(session, incident, run, state)

    async def _apply_decided(
        self, session: AsyncSession, incident: Incident, run: Run, state: MedicState
    ) -> uuid.UUID | None:
        """Apply or skip fixes in order; the first still-pending approval's id, or None.

        A fix whose prerequisite (`depends_on`) was rejected or failed is not run, even if
        approved: a restart without the secret rollback restarts on the bad secret."""
        by_id = {r.fix.fix_id: r for r in state.fixes}
        for rec in sorted(state.fixes, key=lambda r: r.fix.order):
            if rec.status in ("applied", "failed", "rejected") or rec.approval_id is None:
                continue
            blocked = [
                by_id[d]
                for d in rec.fix.depends_on
                if d in by_id and by_id[d].status in ("rejected", "failed")
            ]
            if blocked:
                await self._skip(session, incident, run, state, rec, blocked[0])
                continue
            approval = await session.get(Approval, rec.approval_id, populate_existing=True)
            if approval is None or approval.status == ApprovalStatus.rejected:
                rec.status = "rejected"
                _save(incident, state)
                await session.commit()
                continue
            if approval.status == ApprovalStatus.pending:
                return rec.approval_id
            await self._apply(session, incident, run, state, rec, approval)
        return None

    async def _skip(
        self,
        session: AsyncSession,
        incident: Incident,
        run: Run,
        state: MedicState,
        rec: FixRecord,
        prerequisite: FixRecord,
    ) -> None:
        """Close a fix whose prerequisite was rejected or failed, without running it."""
        why = "was rejected" if prerequisite.status == "rejected" else "failed"
        reason = f"Not run: fix {prerequisite.fix.order} ({prerequisite.fix.title}) {why}."
        approval = await session.get(
            Approval, rec.approval_id, with_for_update=True, populate_existing=True
        )
        if approval is not None and approval.status == ApprovalStatus.pending:
            approval.status = ApprovalStatus.rejected
            approval.decided_at = self.deps.now()
            approval.decided_via = f"Medic: fix {prerequisite.fix.order} {why}"
        rec.status = "rejected"
        rec.applied = FixApplied(fix_id=rec.fix.fix_id, kind=rec.fix.kind, ok=False, error=reason)
        _save(incident, state)
        await record_step(
            session,
            run,
            kind="fix_applied",
            tool_name=rec.fix.action or OPEN_FIX_PR,
            approval_id=rec.approval_id,
            output_summary=reason,
            data=rec.applied,
        )
        await session.commit()

    async def _apply(
        self,
        session: AsyncSession,
        incident: Incident,
        run: Run,
        state: MedicState,
        rec: FixRecord,
        approval: Approval,
    ) -> None:
        rec.status, state.phase = "applying", "applying"
        _save(incident, state)
        await self._status(session, incident, run, "applying")
        await session.commit()
        tool = rec.fix.action or OPEN_FIX_PR
        applied: FixApplied
        try:
            if rec.fix.kind == "emergency" and rec.fix.plan is not None:
                if self.deps.actions is None:
                    raise EmergencyActionError("Emergency actions are not available.")
                token = await issue_approved_action(
                    session,
                    approval,
                    run_id=run.id,
                    action=rec.fix.plan.action,
                    plan_hash=rec.fix.plan.plan_hash(),
                    settings=self.settings,
                )
                result = await self.deps.actions.execute(rec.fix.plan, token, run_id=run.id)
                applied = FixApplied(
                    fix_id=rec.fix.fix_id,
                    kind="emergency",
                    ok=result.ok,
                    action_result=result,
                    error=result.error,
                )
            elif rec.draft is not None:
                if self.deps.prs is None:
                    raise RuntimeError("The GitHub App is not configured.")
                token = await issue_approved_action(
                    session,
                    approval,
                    run_id=run.id,
                    action=OPEN_FIX_PR,
                    plan_hash=rec.draft.plan_hash(),
                    settings=self.settings,
                )
                ref = await self.deps.prs.open_fix_pr(rec.draft, token, run_id=run.id)
                rec.pull_request = ref
                applied = FixApplied(fix_id=rec.fix.fix_id, kind="pr", ok=True, pull_request=ref)
            else:
                raise RuntimeError("Nothing to apply for this fix.")
        except Exception as exc:
            logger.warning("medic fix failed", extra={"fix": rec.fix.fix_id}, exc_info=True)
            applied = FixApplied(
                fix_id=rec.fix.fix_id,
                kind=rec.fix.kind,
                ok=False,
                error=redact(f"{type(exc).__name__}: {exc}")[:500],
            )
        rec.applied = applied
        rec.status = "applied" if applied.ok else "failed"
        _save(incident, state)
        await record_step(
            session,
            run,
            kind="fix_applied",
            tool_name=tool,
            approval_id=approval.id,
            output_summary=(
                f"Applied: {rec.fix.title}" if applied.ok else f"Failed: {applied.error}"
            ),
            data=applied,
        )
        await session.commit()

    async def _after_decisions(
        self, session: AsyncSession, incident: Incident, run: Run, state: MedicState
    ) -> None:
        applied = [r for r in state.fixes if r.status == "applied"]
        if not applied:
            rejected = sum(1 for r in state.fixes if r.status == "rejected")
            reason = (
                "Every fix was rejected."
                if rejected == len(state.fixes)
                else "No fix could be applied."
            )
            await self._fail(
                session,
                incident,
                run,
                state,
                reason,
                run_status=RunStatus.succeeded,
                stop=StopReason.completed,
            )
            return
        if state.phase == "verifying":
            return  # a second resume after the same decisions: verify is already scheduled
        now = self.deps.now()
        state.phase, state.fixes_applied_at = "verifying", now
        state.verify_chain, state.verify_checks = uuid.uuid4().hex, 0
        pr_only = all(r.fix.kind == "pr" for r in applied)
        window = (
            self.settings.medic_pr_verify_timeout_seconds
            if pr_only
            else self.settings.medic_verify_timeout_seconds
        )
        state.verify_deadline = now + timedelta(seconds=window)
        state.healthy_streak = 0
        _save(incident, state)
        await self._status(session, incident, run, "verifying")
        await queue.enqueue(
            session,
            VERIFY_JOB,
            {"incident_id": str(incident.id), "run_id": str(run.id), "chain": state.verify_chain},
            run_after=now + timedelta(seconds=self.settings.medic_verify_interval_seconds),
            max_attempts=1,
        )
        await session.commit()

    # ------------------------------------------------------------------- verify

    async def verify(
        self, incident_id: uuid.UUID, run_id: uuid.UUID, chain: str | None = None
    ) -> None:
        async with self.session_factory() as session:
            loaded = await self._load(session, incident_id, run_id)
            if loaded is None:
                return
            incident, run, state = loaded
            if run.status in TERMINAL_RUN_STATUSES or state.phase != "verifying":
                return
            if chain is not None and state.verify_chain not in (None, chain):
                return  # a duplicate chain
            now = self.deps.now()
            if self.deps.github is not None:
                try:
                    await refresh_pull_requests(incident, self.deps.github, now=now)
                except Exception:
                    logger.warning("verify: PR refresh failed", exc_info=True)
                state = MedicState.model_validate(incident.state or {})
            waiting, deploy_after = self._pr_waits(state)
            graph = await self._graph(session, incident)
            since = state.fixes_applied_at or now
            checked = await self._live_checks(
                session,
                incident,
                graph,
                state,
                since=since,
                deploy_after=deploy_after,
                waiting=waiting,
                check_logs=any(s.rule == "log_error_spike" for s in _signals(incident)),
            )
            result = checked or VerifyResult(
                ok=False,
                checks=[VerifyCheck(name="other", ok=False, detail="no AWS access")],
                duration_ms=0,
            )
            state.verify_checks += 1
            result = result.model_copy(update={"check": state.verify_checks})
            state.verify = result
            state.healthy_streak = state.healthy_streak + 1 if result.ok else 0
            _save(incident, state)
            await record_step(
                session,
                run,
                kind="verify_result",
                output_summary=(
                    f"Healthy (check {state.verify_checks})"
                    if result.ok
                    else f"Waiting for {result.waiting} (check {state.verify_checks})"
                    if result.waiting
                    else f"Not recovered yet (check {state.verify_checks})"
                ),
                data=result,
            )
            await session.commit()
            if result.ok and state.healthy_streak >= HEALTHY_ROUNDS:
                await self._resolve(session, incident, run, state)
            elif state.verify_deadline is not None and now >= state.verify_deadline:
                await self._fail(
                    session,
                    incident,
                    run,
                    state,
                    f"Still waiting for {result.waiting} at the verify deadline."
                    if result.waiting
                    else "Signals did not recover before the verify deadline.",
                    still_failing=[c.detail for c in result.checks if not c.ok],
                    stop=StopReason.completed,
                )
            else:
                await queue.enqueue(
                    session,
                    VERIFY_JOB,
                    {
                        "incident_id": str(incident.id),
                        "run_id": str(run.id),
                        "chain": state.verify_chain,
                    },
                    run_after=now + timedelta(seconds=self.settings.medic_verify_interval_seconds),
                    max_attempts=1,
                )
                await session.commit()

    async def _live_checks(
        self,
        session: AsyncSession,
        incident: Incident,
        graph: fx.Graph,
        state: MedicState,
        *,
        since: datetime,
        deploy_after: datetime | None = None,
        waiting: list[str] | None = None,
        check_logs: bool = True,
    ) -> VerifyResult | None:
        """The recovery checks, now. None when no affected resource can be read."""
        services, databases, logs = self._verify_targets(graph, state)
        deployments = self._k8s_targets(graph, state)
        readers: dict[str, AwsReader] = {}
        for res in [*services, *databases]:
            conn = graph.connections.get(res.connection_id) if res.connection_id else None
            if conn is not None and str(conn.id) not in readers:
                try:
                    readers[str(conn.id)] = await asyncio.to_thread(self.deps.reader_for, conn)
                except Exception:
                    logger.warning("medic: no AWS session", exc_info=True)
        now = self.deps.now()
        k8s_out: list[VerifyCheck] = []
        k8s_waits: list[str] = []
        for dep in deployments:
            conn = graph.connections.get(dep.connection_id) if dep.connection_id else None
            if conn is None:
                continue
            try:
                kube = await asyncio.to_thread(self.deps.kube_for, conn)
            except Exception:
                logger.warning("medic: no kubeconfig", exc_info=True)
                continue
            try:
                k8s_out += await k8s_checks(
                    kube,
                    dep,
                    since=since,
                    now=now,
                    check_logs=check_logs,
                    waits=k8s_waits,
                    deploy_after=deploy_after,
                )
            except Exception as exc:  # an unreadable cluster fails the round, not the run
                detail = f"{dep.name}: could not read the cluster ({str(exc)[:120]})"
                k8s_out.append(VerifyCheck(name="other", ok=False, detail=detail))
            finally:
                close = getattr(kube, "close", None)
                if close is not None:
                    close()
        if not readers and not k8s_out:
            return None
        aws = (
            await asyncio.to_thread(
                run_checks,
                readers,
                services,
                databases,
                logs,
                since=since,
                now=now,
                check_logs=check_logs,
                deploy_after=deploy_after,
                waiting=waiting,
            )
            if readers
            else None
        )
        if not k8s_out:
            return aws
        checks = [*(aws.checks if aws else []), *k8s_out]
        waits = [w for w in [aws.waiting if aws else "; ".join(waiting or []), *k8s_waits] if w]
        return VerifyResult(
            ok=all(c.ok for c in checks) and not waits,
            checks=checks,
            duration_ms=aws.duration_ms if aws else 0,
            waiting="; ".join(waits) or None,
        )

    async def _note_rediagnosis(
        self, session: AsyncSession, incident: Incident, run: Run, signals: list[str]
    ) -> None:
        reason = "New signals, diagnosing again: " + "; ".join(signals)
        await record_step(
            session,
            run,
            kind="incident_updated",
            output_summary=reason[:300],
            data=IncidentUpdated(
                incident_id=incident.id,
                status=incident.status,
                previous_status=incident.status,
                at=self.deps.now(),
                reason=reason[:500],
            ),
        )

    async def rediagnose(
        self, incident_id: uuid.UUID, run_id: uuid.UUID, signals: list[str]
    ) -> None:
        """New signals reached an incident whose fixes wait for approval: the diagnosis is
        stale, so its pending approvals are closed and Medic investigates again. Once a person
        has approved or rejected a fix, the run goes on as it is."""
        async with self.session_factory() as session:
            loaded = await self._load(session, incident_id, run_id)
            if loaded is None:
                return
            incident, run, state = loaded
            await session.get(Run, run.id, with_for_update=True, populate_existing=True)
            in_grace = (
                state.phase == "investigating"
                and state.no_fix_until is not None
                and not state.fixes
            )
            if (
                run.status in TERMINAL_RUN_STATUSES
                or (state.phase != "waiting_approval" and not in_grace)
                or state.rediagnosed >= MAX_REDIAGNOSES
                or any(r.status != "waiting_approval" for r in state.fixes)
            ):
                return
            for rec in state.fixes:
                approval = (
                    await session.get(
                        Approval, rec.approval_id, with_for_update=True, populate_existing=True
                    )
                    if rec.approval_id
                    else None
                )
                if approval is not None and approval.status != ApprovalStatus.pending:
                    return  # decided meanwhile
            # The approvals stay pending: the re-diagnosis keeps them if it proposes the same
            # fixes, and closes them otherwise (_settle_superseded).
            state.superseded = list(state.fixes)
            state.fixes, state.notes, state.phase = [], [], "investigating"
            state.no_fix_until = None
            state.rediagnosed += 1
            _save(incident, state)
            await self._status(
                session, incident, run, "investigating", "new signals, diagnosing again"
            )
            await self._note_rediagnosis(session, incident, run, signals)
            run.status, run.stop_reason = RunStatus.queued, None
            await session.commit()
        await self.investigate(incident_id, run_id)

    @staticmethod
    def _pr_waits(state: MedicState) -> tuple[list[str], datetime | None]:
        """Fix PRs not merged yet (verify waits), and the latest merge time (verify waits for a
        deploy that started after it). Only when PRs were the only fixes applied."""
        applied = [r for r in state.fixes if r.status == "applied"]
        if not applied or any(r.fix.kind != "pr" for r in applied):
            return [], None
        prs = [r.pull_request for r in applied if r.pull_request is not None]
        waits = [f"{p.repo}#{p.number} to be merged" for p in prs if p.state == "open"]
        merged = [p.merged_at for p in prs if p.state == "merged" and p.merged_at is not None]
        return waits, max(merged) if merged else None

    def _k8s_targets(self, graph: fx.Graph, state: MedicState) -> list[Resource]:
        """Kubernetes Deployments that are affected or that an applied fix acted on."""
        ids = set(graph.affected)
        acted = {
            (r.fix.plan.params.get("deployment") or "")
            for r in state.fixes
            if r.status == "applied" and r.fix.plan is not None
        }
        return [
            r
            for r in graph.resources.values()
            if r.type == "k8s:deployment" and (r.id in ids or r.name in acted)
        ]

    def _verify_targets(
        self, graph: fx.Graph, state: MedicState
    ) -> tuple[list[Resource], list[Resource], dict[str, list[str]]]:
        ids = set(graph.affected)
        acted = {
            (r.fix.plan.params.get("service") or "")
            for r in state.fixes
            if r.status == "applied" and r.fix.plan is not None
        }
        services = [
            r
            for r in graph.resources.values()
            if r.type == "ecs:service" and (r.id in ids or r.name in acted)
        ]
        # The databases the affected services read: slow queries show up there (missing index).
        reads = {e.to_id for e in graph.edges if e.kind == "reads" and e.from_id in ids}
        databases = [
            r
            for r in graph.resources.values()
            if r.type == "rds:db" and (r.id in ids or r.id in reads)
        ]
        logs: dict[str, list[str]] = {}
        for e in graph.edges:
            target = graph.resources.get(e.to_id)
            if e.kind == "logs_to" and target is not None and target.type == "logs:log-group":
                logs.setdefault(str(e.from_id), []).append(target.name)
        return services, databases, logs

    async def _resolve(
        self,
        session: AsyncSession,
        incident: Incident,
        run: Run,
        state: MedicState,
        reason: str = "Signals recovered for two checks in a row.",
    ) -> None:
        now = self.deps.now()
        await self._close_superseded(session, state)
        rc = state.root_cause
        mem.remember(
            incident, rc.summary if rc else incident.title, rc.root_cause if rc else "", None
        )
        state.phase = "done"
        state.outcome = Outcome(result="resolved", reason=reason)
        _save(incident, state)
        await self._status(session, incident, run, "resolved", "signals recovered")
        # A fix PR not deployed yet, or a rollback with no fix PR: the repo still has the bad
        # change. Track it; an open fix PR keeps the incident out of memory until it is merged
        # or closed.
        created = await fu.create_on_resolve(session, incident, state, now)
        fu.remember_if_settled(incident, fu.holds_memory(created), now)
        if created:
            pending = ", ".join(
                f"{f.repo}#{f.number}"
                if f.kind == "fix_pr"
                else f"commit {(f.triggering_commit or '')[:7]} still in {f.repo}"
                for f in created
            )
            await fu.record_change(session, incident, run, f"Permanent fix pending: {pending}", now)
        applied = [r for r in state.fixes if r.status == "applied"]
        detect_ms = (
            int((incident.created_at - incident.first_signal_at).total_seconds() * 1000)
            if incident.first_signal_at and incident.created_at
            else None
        )
        await record_step(
            session,
            run,
            kind="incident_resolved",
            output_summary="Resolved: signals recovered",
            data=IncidentResolved(
                incident_id=incident.id,
                time_to_detect_ms=max(detect_ms, 0) if detect_ms is not None else None,
                duration_ms=int((now - incident.created_at).total_seconds() * 1000),
                fixes_applied=[r.fix.action or "pull_request" for r in applied],
                pull_requests=[r.pull_request for r in applied if r.pull_request],
                memory_reused=state.memory is not None,
                cost_usd=float(run.cost_usd) if run.cost_usd is not None else None,
                ultra_equiv_usd=float(run.ultra_equiv_usd)
                if run.ultra_equiv_usd is not None
                else None,
            ),
        )
        await self._finish(
            session,
            run,
            RunStatus.succeeded,
            StopReason.completed,
            result=rc.summary if rc else "Resolved",
        )
        await session.commit()


async def fail_without_deps(
    session_factory: SessionFactory, run_id: uuid.UUID, reason: str
) -> None:
    """The investigation cannot start (Token Factory not configured, ...): fail the incident
    and its run instead of leaving them queued."""
    async with session_factory() as session:
        run = await session.get(Run, run_id)
        incident = await session.get(Incident, run.incident_id) if run and run.incident_id else None
        if run is None or incident is None or run.status in TERMINAL_RUN_STATUSES:
            return
        state = MedicState.model_validate(incident.state or {})
        state.phase, state.error = "done", reason
        state.outcome = Outcome(result="failed", reason=reason)
        _save(incident, state)
        now = datetime.now(UTC)
        await set_status(session, incident, run, "failed", reason=reason, now=now)
        await record_step(
            session,
            run,
            kind="incident_failed",
            output_summary=reason,
            data=IncidentFailed(incident_id=incident.id, reason=reason),
        )
        run.status, run.stop_reason, run.error, run.finished_at = (
            RunStatus.failed,
            StopReason.error,
            reason,
            now,
        )
        await session.commit()
