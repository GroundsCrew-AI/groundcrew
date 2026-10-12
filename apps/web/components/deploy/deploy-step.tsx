"use client";

import { cn } from "cn";
import { Check, ExternalLink, Map as MapIcon, RotateCcw, X } from "lucide-react";
import Link from "next/link";

import type { Step } from "@/lib/api/client";
import { stageLabel, targetLabel } from "@/lib/deploys";
import { formatCost, formatDuration } from "@/lib/format";

import { BuildAttemptCard } from "./build-attempt";

/** Rail colour for a deploy step in the shared timeline. */
export function deployStepColor(step: Step): string | null {
  const data = step.data;
  if (!data) return null;
  switch (data.type) {
    case "stage_finished":
      return data.status === "failed" ? "var(--status-failed)" : "var(--status-ok)";
    case "build_attempt":
      return data.ok ? "var(--status-ok)" : "var(--status-failed)";
    case "verify_result":
      return data.ok ? "var(--status-ok)" : "var(--status-failed)";
    case "live_url":
      return "var(--status-ok)";
    case "rollback_offered":
      return "var(--status-failed)";
    case "run_finished":
      return data.status === "succeeded" ? "var(--status-ok)" : "var(--status-failed)";
    default:
      return "var(--subtle)";
  }
}

/**
 * Steps the timeline hides: a stage start once that stage has finished (its row says it all).
 * A stage can run again (preflight after the env changed), so only a later finish counts.
 */
export function hiddenDeploySteps(steps: Step[]): Set<number> {
  const hidden = new Set<number>();
  const open = new Map<string, number>();
  for (const s of [...steps].sort((a, b) => a.seq - b.seq)) {
    if (s.data?.type === "stage_started") open.set(s.data.stage, s.seq);
    if (s.data?.type === "stage_finished") {
      const started = open.get(s.data.stage);
      if (started !== undefined) hidden.add(started);
      open.delete(s.data.stage);
    }
  }
  return hidden;
}

/**
 * Approvals whose deploy plan was replaced by a later one (the env changed at the approval
 * card, so preflight ran again). Their cards collapse to a note instead of "Rejected".
 */
export function replacedPlans(steps: Step[]): Set<string> {
  const plans = [...steps]
    .sort((a, b) => a.seq - b.seq)
    .filter((s) => s.data?.type === "deploy_plan" && !s.data.rollback_to_revision && s.approval_id);
  return new Set(plans.slice(0, -1).map((s) => s.approval_id!));
}

/** A deploy event in the run timeline, rendered from its typed `data`. */
export function DeployStep({ step }: { step: Step }) {
  const data = step.data;
  if (!data) return null;
  switch (data.type) {
    case "stage_started":
      return (
        <p className="text-[13px]">
          <span className="font-medium">{stageLabel(data.stage)}</span>{" "}
          <span className="text-muted-foreground">started</span>
        </p>
      );
    case "stage_finished":
      return (
        <div className="text-[13px]">
          <p className="flex flex-wrap items-baseline gap-x-2">
            <span className="font-medium">{stageLabel(data.stage)}</span>
            <span
              className={cn(
                data.status === "failed" ? "text-[var(--status-failed)]" : "text-muted-foreground",
              )}
            >
              {data.status === "succeeded" ? "done" : data.status}
            </span>
            <span className="tabular ml-auto font-mono text-[11.5px] text-muted-foreground">
              {formatDuration(data.duration_ms)}
            </span>
          </p>
          {data.summary && <p className="mt-0.5 text-[12.5px] text-muted-foreground">{data.summary}</p>}
        </div>
      );
    case "build_attempt":
      return <BuildAttemptCard attempt={data} />;
    case "verify_result":
      return (
        <div className="text-[13px]">
          <p className="flex items-baseline gap-2">
            <span className="font-medium">Verify</span>
            <span className={data.ok ? "text-[var(--status-ok)]" : "text-[var(--status-failed)]"}>
              {data.ok ? "Passed" : "Failed"}
            </span>
            <span className="tabular ml-auto font-mono text-[11.5px] text-muted-foreground">
              {formatDuration(data.duration_ms)}
            </span>
          </p>
          <ul className="mt-1 space-y-0.5 text-[12.5px]">
            {data.checks.map((c) => (
              <li key={`${c.name}-${c.url ?? ""}`} className="flex items-baseline gap-1.5">
                {c.ok ? (
                  <Check aria-label="passed" className="size-3 shrink-0 text-[var(--status-ok)]" />
                ) : (
                  <X aria-label="failed" className="size-3 shrink-0 text-[var(--status-failed)]" />
                )}
                <span className="capitalize">{c.name}</span>
                <span className="text-muted-foreground">{c.detail}</span>
                {c.url && <span className="truncate font-mono text-[11.5px] text-subtle">{c.url}</span>}
              </li>
            ))}
          </ul>
          {data.logs_tail && (
            <pre className="mt-2 max-h-60 overflow-auto rounded-md border bg-background px-3 py-2 font-mono text-[12px] whitespace-pre-wrap">
              {data.logs_tail}
            </pre>
          )}
        </div>
      );
    case "live_url":
      return (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-[color-mix(in_oklab,var(--status-ok)_40%,var(--border))] bg-card px-3 py-2.5">
          <span className="text-[13px] font-medium">Live on {targetLabel(data.target)}</span>
          <a
            href={data.url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 font-mono text-[12.5px] underline-offset-4 hover:underline"
          >
            {data.url}
            <ExternalLink aria-hidden="true" className="size-3" />
            <span className="sr-only">(opens in a new tab)</span>
          </a>
          <Link
            href={data.resource_id ? `/map?resource=${data.resource_id}` : "/map"}
            className="ml-auto inline-flex items-center gap-1 text-[12.5px] text-muted-foreground hover:text-foreground"
          >
            <MapIcon aria-hidden="true" className="size-3.5" />
            View on Map
          </Link>
        </div>
      );
    case "rollback_offered":
      return (
        <div className="text-[13px]">
          <p className="flex items-center gap-1.5 font-medium">
            <RotateCcw aria-hidden="true" className="size-3.5 text-[var(--status-failed)]" />
            Rollback offered
          </p>
          <p className="mt-0.5 text-[12.5px] text-muted-foreground">
            {data.reason} {data.to_revision ? `Back to ${data.to_revision}.` : "This removes the new deployment."}
          </p>
        </div>
      );
    case "run_finished": {
      const cost = formatCost(data.cost_usd);
      return (
        <div className="text-[13px]">
          <p className="font-medium">
            {data.status === "succeeded" && data.live_url ? "Deployed" : "Deploy ended"}
          </p>
          <p className="mt-0.5 text-[12.5px] text-muted-foreground">
            {data.build_attempts} build {data.build_attempts === 1 ? "attempt" : "attempts"}
            {data.automatic_fix && ", fixed automatically"}
            {data.duration_ms !== null && `, ${formatDuration(data.duration_ms)}`}
            {cost && `, ${cost} in model calls`}
          </p>
          {data.error && <p className="mt-0.5 text-[12.5px] text-[var(--status-failed)]">{data.error}</p>}
        </div>
      );
    }
    default:
      return null;
  }
}
