import type { Approval, Step } from "@/lib/api/client";
import {
  type BuildAttempt,
  type DeployPlan,
  type DeployStage,
  type LiveUrl,
  type RollbackOffered,
  type RunFinished,
  STAGES,
  type VerifyResult,
} from "@/lib/deploys";

export type StageStatus = "pending" | "running" | "waiting" | "succeeded" | "failed" | "skipped";

export type StageView = {
  stage: DeployStage;
  status: StageStatus;
  durationMs: number | null;
  summary: string | null;
};

export type DeployView = {
  stages: StageView[];
  /** The stage running or waiting now, else the last one reached. */
  current: DeployStage | null;
  attempts: BuildAttempt[];
  /** A build passed after Builder changed the Dockerfile because of a failure. */
  automaticFix: boolean;
  plan: DeployPlan | null;
  planApproval: Approval | null;
  verify: VerifyResult | null;
  liveUrl: LiveUrl | null;
  rollback: RollbackOffered | null;
  finished: RunFinished | null;
};

/**
 * Everything the deploy view shows, derived from the run's steps (each deploy event is a step
 * with typed `data`) and its approvals. Pure and order-independent within seq order, so a
 * replayed or reloaded stream gives the same view.
 */
export function deployView(steps: Step[], approvals: Record<string, Approval>): DeployView {
  const byStage = new Map<DeployStage, StageView>();
  let current: DeployStage | null = null;
  const attempts = new Map<number, BuildAttempt>();
  let plan: DeployPlan | null = null;
  let planApproval: Approval | null = null;
  let verify: VerifyResult | null = null;
  let liveUrl: LiveUrl | null = null;
  let rollback: RollbackOffered | null = null;
  let finished: RunFinished | null = null;

  for (const step of [...steps].sort((a, b) => a.seq - b.seq)) {
    const data = step.data;
    if (!data) continue;
    switch (data.type) {
      case "stage_started":
        byStage.set(data.stage, { stage: data.stage, status: "running", durationMs: null, summary: null });
        current = data.stage;
        break;
      case "stage_finished":
        byStage.set(data.stage, {
          stage: data.stage,
          status: data.status,
          durationMs: data.duration_ms,
          summary: data.summary ?? null,
        });
        current = data.stage;
        break;
      case "build_attempt":
        attempts.set(data.attempt, data);
        break;
      case "deploy_plan":
        // A rollback plan is shown on its own card; the deploy plan is the first one.
        if (data.rollback_to_revision) break;
        plan = data;
        planApproval = step.approval_id ? (approvals[step.approval_id] ?? null) : null;
        break;
      case "verify_result":
        verify = data;
        break;
      case "live_url":
        liveUrl = data;
        break;
      case "rollback_offered":
        rollback = data;
        break;
      case "run_finished":
        finished = data;
        break;
    }
  }

  const approvalStage = byStage.get("approval");
  if (approvalStage?.status === "running" && planApproval?.status === "pending") {
    byStage.set("approval", { ...approvalStage, status: finished ? "skipped" : "waiting" });
  }
  const order = byStage.has("rollback") ? [...STAGES, "rollback" as const] : STAGES;
  const stages = order.map(
    (stage) => byStage.get(stage) ?? { stage, status: "pending" as const, durationMs: null, summary: null },
  );
  const sorted = [...attempts.values()].sort((a, b) => a.attempt - b.attempt);
  const passed = sorted.find((a) => a.ok);
  return {
    stages,
    current,
    attempts: sorted,
    automaticFix: finished?.automatic_fix ?? (passed !== undefined && passed.dockerfile_source === "fixed"),
    plan,
    planApproval,
    verify,
    liveUrl,
    rollback,
    finished,
  };
}

/** Preflight checks that failed and were not overridden: while any remain, no approval. */
export function failingChecks(plan: DeployPlan): DeployPlan["preflight"] {
  return plan.preflight.filter((check) => check.status === "fail" && !check.overridden);
}

/** True when a preflight check failed and was not overridden: the plan cannot be approved. */
export function planBlocked(plan: DeployPlan): boolean {
  return failingChecks(plan).length > 0;
}

/** Masked display value for an env var on the approval card. */
export function envDisplay(v: DeployPlan["env"][number]): string {
  if (v.secret) return v.provided ? "********" : "not set";
  return v.provided ? (v.value ?? "") : "not set";
}
