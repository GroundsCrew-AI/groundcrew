import { cn } from "cn";
import Link from "next/link";
import type { CSSProperties } from "react";

import { type DeployState, type DeploySummary, targetLabel } from "@/lib/deploys";
import { elapsedMs, formatCost, formatDuration, formatRelative, formatTimestamp } from "@/lib/format";

const STATES: Record<DeployState, { label: string; color: string }> = {
  queued: { label: "Queued", color: "var(--status-neutral)" },
  running: { label: "Running", color: "var(--status-running)" },
  waiting_approval: { label: "Needs approval", color: "var(--status-waiting)" },
  live: { label: "Live", color: "var(--status-ok)" },
  failed: { label: "Failed", color: "var(--status-failed)" },
  rolled_back: { label: "Rolled back", color: "var(--status-waiting)" },
  stopped: { label: "Stopped", color: "var(--status-neutral)" },
};

export function DeployStatePill({ state, className }: { state: DeployState; className?: string }) {
  const { label, color } = STATES[state];
  const live = state === "running" || state === "queued";
  return (
    <span
      className={cn(
        "tint inline-flex h-5 items-center gap-1.5 rounded-full px-2 text-[12px] font-medium whitespace-nowrap",
        className,
      )}
      style={{ "--tint": color } as CSSProperties}
    >
      <span aria-hidden="true" className={cn("size-1.5 rounded-full bg-current", state === "running" && "animate-live")} />
      {label}
      {live && <span className="sr-only">, in progress</span>}
    </span>
  );
}

const GRID = "sm:grid-cols-[112px_minmax(0,1.3fr)_minmax(0,1fr)_minmax(0,1.2fr)_64px_64px_80px]";

export function DeployListHeader() {
  return (
    <div aria-hidden="true" className={cn("hidden gap-x-4 border-b px-4 py-2 text-[12px] text-subtle sm:grid", GRID)}>
      <span>Status</span>
      <span>Source</span>
      <span>Target</span>
      <span>URL</span>
      <span className="text-right">Duration</span>
      <span className="text-right">Cost</span>
      <span className="text-right">Started</span>
    </div>
  );
}

/** Deploy rows: status, source, target, live URL, duration, cost, when. */
export function DeployList({ deploys, now = Date.now() }: { deploys: DeploySummary[]; now?: number }) {
  return (
    <ul className="divide-y divide-border">
      {deploys.map((d) => {
        const duration = d.duration_ms ?? elapsedMs(d.started_at, d.finished_at, now);
        return (
          <li key={d.id}>
            <Link
              href={`/deploy/${d.id}`}
              className={cn(
                "grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-4 gap-y-1 px-4 py-2.5 hover:bg-accent/60 focus-visible:bg-accent/60",
                GRID,
              )}
            >
              <span>
                <DeployStatePill state={d.status} />
              </span>
              <span
                className="min-w-0 [overflow-wrap:anywhere]"
                title={`${d.source.repo}${d.source.ref ? `@${d.source.ref}` : ""}`}
              >
                {d.source.repo}
                {d.source.ref && <span className="text-muted-foreground">@{d.source.ref}</span>}
              </span>
              <span className="hidden min-w-0 truncate text-muted-foreground sm:block">
                {d.target.name} <span className="text-subtle">({targetLabel(d.target.kind)})</span>
              </span>
              <span className="hidden min-w-0 truncate font-mono text-[12px] text-muted-foreground sm:block">
                {d.live_url ?? ""}
              </span>
              <span className="tabular hidden text-right text-[12px] text-muted-foreground sm:block">
                {duration !== null ? formatDuration(duration) : ""}
              </span>
              <span className="tabular hidden text-right font-mono text-[12px] text-muted-foreground sm:block">
                {formatCost(d.cost_usd) ?? ""}
              </span>
              <time dateTime={d.created_at} title={formatTimestamp(d.created_at)} className="text-right text-[12px] text-muted-foreground">
                {formatRelative(d.created_at, now)}
              </time>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
