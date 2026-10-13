import type { Step } from "@/lib/api/client";
import type { EvidenceItem, IncidentSummary, ProposedFix, RootCause } from "@/lib/incidents";

import { step } from "./fixtures";

type Data = NonNullable<Step["data"]>;

export function medicStep(seq: number, data: Data, overrides: Partial<Step> = {}): Step {
  return step(seq, { agent: "medic", kind: data.type, tool_name: null, tool_policy: null, data, ...overrides });
}

export const EVIDENCE: EvidenceItem[] = [
  {
    id: "ev-aaaaaaaaaa",
    source: "ecs:checkout-worker",
    timestamp: "2026-10-12T09:58:00+00:00",
    resource_id: "res-worker",
    line: "task a stopped: OutOfMemoryError: Container killed due to memory usage; exit code 137 [OOM]",
  },
  {
    id: "ev-bbbbbbbbbb",
    source: "github:GroundsCrew-AI/checkout-demo",
    timestamp: "2026-10-12T09:49:00+00:00",
    resource_id: "res-repo",
    line: "commit 9f8e7d6 by dev: Tune worker memory | files: worker/ecs-task.json",
  },
];

export const ROOT_CAUSE: RootCause = {
  summary: "Worker tasks are OOM-killed since commit 9f8e7d6 set a 24 MiB limit.",
  root_cause: "worker/ecs-task.json sets container_memory 24; every task exits 137.",
  claims: [
    { text: "Worker tasks exit 137 (OOM).", evidence_ids: ["ev-aaaaaaaaaa"] },
    { text: "Commit 9f8e7d6 lowered the limit.", evidence_ids: ["ev-bbbbbbbbbb"] },
  ],
  triggering_change: { kind: "commit", ref: "9f8e7d6", at: "2026-10-12T09:49:00+00:00", description: "Tune worker memory", evidence_ids: [] },
  affected_resources: ["checkout-worker"],
  confidence: "high",
  fix_options: [],
  search_query: "linux oom killer exit code 137",
};

export const ROLLBACK: ProposedFix = {
  fix_id: "fix-1",
  kind: "emergency",
  order: 1,
  title: "Roll back checkout-worker to checkout-worker:41",
  rationale: "Production is down now; the previous revision ran without these failures.",
  action: "ecs_rollback_service",
  plan: null,
  plan_lines: [{ what: "checkout-worker task definition", before: "checkout-worker:42", after: "checkout-worker:41" }],
  pull_request: null,
  approval_id: "approval-1",
};

export const PR_FIX: ProposedFix = {
  fix_id: "fix-2",
  kind: "pr",
  order: 2,
  title: "Restore the worker's memory limit",
  rationale: "The 24 MiB limit kills every worker task.",
  action: null,
  plan: null,
  plan_lines: [],
  pull_request: {
    repo: "GroundsCrew-AI/checkout-demo",
    base: "main",
    branch: "groundcrew/fix-1a2b3c4d",
    title: "Restore the worker's memory limit",
    body: "b",
    files: [
      {
        path: "worker/ecs-task.json",
        diff: '--- a/worker/ecs-task.json\n+++ b/worker/ecs-task.json\n-{"memory": "512", "container_memory": 24}\n+{"memory": "512"}',
      },
    ],
  },
  approval_id: "approval-2",
};

export function incidentSummary(overrides: Partial<IncidentSummary> = {}): IncidentSummary {
  return {
    id: "inc-1",
    workspace_id: "ws-1",
    run_id: "run-1",
    title: "checkout-worker: 3 tasks stopped with exit code 137",
    status: "waiting_approval",
    severity: "high",
    detected_by: "watcher",
    detector_rule: "ecs_tasks_stopped",
    opened_at: "2026-10-12T10:00:00Z",
    resolved_at: null,
    window: { start: "2026-10-12T09:30:00Z", end: null },
    affected_resource_ids: ["res-worker"],
    affected_resources: [{ id: "res-worker", name: "checkout-worker", type: "ecs:service", provider: "aws" }],
    root_cause_summary: null,
    triggering_change: null,
    fixes_applied: [],
    pull_requests: [],
    memory_reused: false,
    prompt_tokens: 1000,
    completion_tokens: 200,
    total_tokens: 1200,
    cost_usd: 0.0042,
    time_to_detect_ms: 60000,
    duration_ms: null,
    ...overrides,
  };
}
