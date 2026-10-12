"use client";

import { cn } from "cn";
import { Check, Clock, X } from "lucide-react";

import type { StageView } from "@/lib/deploy-view";
import { type DeployStage, stageLabel } from "@/lib/deploys";

const LABEL: Record<StageView["status"], string> = {
  pending: "not started",
  running: "running",
  waiting: "waiting for approval",
  succeeded: "done",
  failed: "failed",
  skipped: "skipped",
};

/** The pipeline's stages in order, with the one running now marked. */
export function StageStrip({ stages, current }: { stages: StageView[]; current: DeployStage | null }) {
  return (
    <ol aria-label="Deploy stages" className="flex flex-wrap items-center gap-x-1 gap-y-2">
      {stages.map((s, i) => (
        <li
          key={s.stage}
          aria-current={s.stage === current && (s.status === "running" || s.status === "waiting") ? "step" : undefined}
          className="flex items-center gap-1"
        >
          <span
            className={cn(
              "inline-flex h-6 items-center gap-1 rounded-full border px-2 text-[12px]",
              s.status === "pending" && "text-subtle",
              s.status === "running" && "border-[var(--status-running)] text-foreground",
              s.status === "waiting" && "border-[var(--status-waiting)] text-foreground",
              s.status === "failed" && "border-[var(--status-failed)] text-[var(--status-failed)]",
              s.status === "succeeded" && "text-muted-foreground",
            )}
          >
            {s.status === "succeeded" && <Check aria-hidden="true" className="size-3 text-[var(--status-ok)]" />}
            {s.status === "failed" && <X aria-hidden="true" className="size-3" />}
            {s.status === "waiting" && <Clock aria-hidden="true" className="size-3 text-[var(--status-waiting)]" />}
            {s.status === "running" && (
              <span aria-hidden="true" className="animate-live size-1.5 rounded-full bg-[var(--status-running)]" />
            )}
            {stageLabel(s.stage)}
            <span className="sr-only">: {LABEL[s.status]}</span>
          </span>
          {i < stages.length - 1 && <span aria-hidden="true" className="h-px w-2 bg-rail" />}
        </li>
      ))}
    </ol>
  );
}
