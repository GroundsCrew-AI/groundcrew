import type { Approval, RunDetail, Step } from "@/lib/api/client";

export function step(seq: number, overrides: Partial<Step> = {}): Step {
  return {
    id: `step-${seq}`,
    seq,
    agent: "echo_ops",
    kind: "tool",
    model_tier: null,
    model_id: null,
    tool_name: "list_fake_services",
    tool_policy: "read",
    approval_id: null,
    input_summary: null,
    output_summary: `output ${seq}`,
    prompt_tokens: null,
    completion_tokens: null,
    latency_ms: 3,
    cost_usd: null,
    created_at: "2026-10-06T12:00:00Z",
    ...overrides,
  };
}

export function approval(overrides: Partial<Approval> = {}): Approval {
  return {
    id: "approval-1",
    run_id: "run-1",
    workspace_id: "ws-1",
    run_agent: "echo_ops",
    run_goal: "List the services and restart the one named api.",
    tool_name: "restart_fake_service",
    policy: "write",
    arguments: { name: "api" },
    status: "pending",
    decided_by: null,
    decided_by_login: null,
    decided_by_name: null,
    decided_by_avatar_url: null,
    decided_at: null,
    executed_at: null,
    created_at: "2026-10-06T12:00:00Z",
    ...overrides,
  };
}

export function runDetail(overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    id: "run-1",
    workspace_id: "ws-1",
    agent: "echo_ops",
    goal: "List the services and restart the one named api.",
    status: "running",
    stop_reason: null,
    result: null,
    error: null,
    prompt_tokens: 100,
    completion_tokens: 20,
    total_tokens: 120,
    cost_usd: null,
    created_at: "2026-10-06T12:00:00Z",
    started_at: "2026-10-06T12:00:00Z",
    finished_at: null,
    duration_ms: null,
    rejected_actions: 0,
    steps: [],
    approvals: [],
    ...overrides,
  };
}
