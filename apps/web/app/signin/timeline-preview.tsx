"use client";

import { useEffect, useState } from "react";

import { StatusPill } from "@/components/runs/badges";
import { Timeline } from "@/components/runs/timeline";
import type { Approval, Step } from "@/lib/api/client";

const GOAL = "The api service is failing its health checks. Find the cause and fix it.";

/** A finished incident fix, drawn with the real timeline components. Decorative only. */
function fixture(now: number): { steps: Step[]; approval: Approval } {
  const at = (secondsAgo: number) => new Date(now - secondsAgo * 1000).toISOString();
  const base = {
    agent: "medic",
    model_tier: null,
    model_id: null,
    tool_name: null,
    tool_policy: null,
    approval_id: null,
    input_summary: null,
    output_summary: null,
    prompt_tokens: null,
    completion_tokens: null,
    latency_ms: null,
    cost_usd: null,
  } satisfies Partial<Step>;
  const approval: Approval = {
    id: "preview-approval",
    run_id: "preview",
    workspace_id: "preview",
    run_agent: "medic",
    run_goal: GOAL,
    tool_name: "ecs_rollback_service",
    policy: "emergency",
    arguments: { service: "api", task_definition: "api:41" },
    status: "approved",
    decided_by: "preview-user",
    decided_by_login: "you",
    decided_by_name: "You",
    decided_by_avatar_url: null,
    decided_via: null,
    decided_at: at(40),
    executed_at: at(39),
    created_at: at(52),
  };
  const steps: Step[] = [
    {
      ...base,
      id: "1",
      seq: 1,
      kind: "llm",
      model_tier: "super",
      model_id: "nvidia/Llama-3_3-Nemotron-Super-49B-v1_5",
      output_summary: 'tool calls: read_service_events({"service": "api"})',
      prompt_tokens: 2140,
      completion_tokens: 36,
      latency_ms: 1240,
      created_at: at(55),
    },
    {
      ...base,
      id: "2",
      seq: 2,
      kind: "tool",
      tool_name: "read_service_events",
      tool_policy: "read",
      output_summary: "3 tasks stopped with exit code 137 (out of memory) since deploy api:42",
      latency_ms: 310,
      created_at: at(54),
    },
    {
      ...base,
      id: "3",
      seq: 3,
      kind: "approval_requested",
      tool_name: "ecs_rollback_service",
      approval_id: approval.id,
      created_at: at(52),
    },
    {
      ...base,
      id: "4",
      seq: 4,
      kind: "tool",
      tool_name: "ecs_rollback_service",
      tool_policy: "emergency",
      output_summary: "api rolled back to api:41; 2 of 2 tasks healthy",
      latency_ms: 840,
      created_at: at(39),
    },
  ];
  return { steps, approval };
}

export function TimelinePreview() {
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => setNow(Date.now()), []);
  if (now === null) return null;
  const { steps, approval } = fixture(now);
  return (
    <div className="w-full max-w-[480px] px-10">
      <div className="mb-5 flex items-center justify-between gap-3">
        <p className="text-[13px] font-medium">{GOAL}</p>
        <StatusPill status="succeeded" />
      </div>
      <div className="pointer-events-none select-none">
        <Timeline
          steps={steps}
          approvals={{ [approval.id]: approval }}
          status="succeeded"
          result="Deploy api:42 lowered the memory limit, so its tasks ran out of memory. Rolled back to api:41 and opened a pull request that restores the limit."
          onDecide={() => undefined}
          deciding={null}
          animateAfter={Number.MAX_SAFE_INTEGER}
        />
      </div>
    </div>
  );
}
