"use client";

import { useEffect, useState } from "react";

import { StatusPill } from "@/components/runs/badges";
import { Timeline } from "@/components/runs/timeline";
import type { Approval, Step } from "@/lib/api/client";

/** A finished Echo Ops run, drawn with the real timeline components. Decorative only. */
function fixture(now: number): { steps: Step[]; approval: Approval } {
  const at = (secondsAgo: number) => new Date(now - secondsAgo * 1000).toISOString();
  const base = {
    agent: "echo_ops",
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
    run_agent: "echo_ops",
    run_goal: "List the services and restart the one named api.",
    tool_name: "restart_fake_service",
    policy: "write",
    arguments: { name: "api" },
    status: "approved",
    decided_by: "preview-user",
    decided_by_login: "you",
    decided_by_name: "You",
    decided_by_avatar_url: null,
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
      model_tier: "nano",
      model_id: "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
      output_summary: "tool calls: list_fake_services({})",
      prompt_tokens: 498,
      completion_tokens: 17,
      latency_ms: 777,
      created_at: at(55),
    },
    {
      ...base,
      id: "2",
      seq: 2,
      kind: "tool",
      tool_name: "list_fake_services",
      tool_policy: "read",
      output_summary: "api, worker, web",
      latency_ms: 2,
      created_at: at(54),
    },
    {
      ...base,
      id: "3",
      seq: 3,
      kind: "approval_requested",
      tool_name: "restart_fake_service",
      approval_id: approval.id,
      created_at: at(52),
    },
    {
      ...base,
      id: "4",
      seq: 4,
      kind: "tool",
      tool_name: "restart_fake_service",
      tool_policy: "write",
      output_summary: "restarted api",
      latency_ms: 8,
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
        <p className="text-[13px] font-medium">List the services and restart the one named api.</p>
        <StatusPill status="succeeded" />
      </div>
      <div className="pointer-events-none select-none">
        <Timeline
          steps={steps}
          approvals={{ [approval.id]: approval }}
          status="succeeded"
          result="Services listed: api, worker, web. The api service has been restarted."
          onDecide={() => undefined}
          deciding={null}
          animateAfter={Number.MAX_SAFE_INTEGER}
        />
      </div>
    </div>
  );
}
