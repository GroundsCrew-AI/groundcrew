# telemetry

Read-only evidence for Medic (Phase 5). Contract: `docs/phase5-interfaces.md`, section 1.

- `evidence.py`: `Line` (`ISO | source | resource | message`, redacted, capped), `ReaderOutput`, `render()`.
- Readers, each returning lines and change markers: `cloudwatch_logs.py` (errors with context, latency
  summaries), `ecs.py` (events, rollouts, stopped tasks and exit codes), `rds.py` (metrics, spikes marked),
  `secrets.py` (metadata only), `github.py` (commits with files, workflow runs), `vercel.py` (deployments).
- `timeline.py`: all sources merged in time order.
- `collect.py`: `collect()` / `GraphEvidenceCollector` follow the graph from the affected resources to their
  neighbors and run the matching readers in parallel.
