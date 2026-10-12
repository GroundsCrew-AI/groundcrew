import { cn } from "cn";
import Link from "next/link";
import type { CSSProperties } from "react";

import { formatCost, formatDuration, formatRelative, formatTimestamp } from "@/lib/format";
import {
  type IncidentStatus,
  type IncidentSummary,
  ruleLabel,
  SEVERITY,
  type Severity,
  STATUS,
} from "@/lib/incidents";

export function IncidentStatusPill({ status, className }: { status: IncidentStatus; className?: string }) {
  const { label, color } = STATUS[status];
  const working = status === "investigating" || status === "applying" || status === "verifying";
  return (
    <span
      className={cn(
        "tint inline-flex h-5 items-center gap-1.5 rounded-full px-2 text-[11.5px] font-medium whitespace-nowrap",
        className,
      )}
      style={{ "--tint": color } as CSSProperties}
    >
      <span aria-hidden="true" className={cn("size-1.5 rounded-full bg-current", working && "animate-live")} />
      {label}
      {working && <span className="sr-only">, in progress</span>}
    </span>
  );
}

export function SeverityTag({ severity }: { severity: Severity }) {
  const { label, color } = SEVERITY[severity];
  return (
    <span className="text-[12px] font-medium" style={{ color }}>
      {label}
    </span>
  );
}

const GRID = "sm:grid-cols-[124px_minmax(0,2fr)_minmax(0,1fr)_72px_64px_80px]";

export function IncidentListHeader() {
  return (
    <div aria-hidden="true" className={cn("hidden gap-x-4 border-b px-4 py-2 text-[11.5px] text-subtle sm:grid", GRID)}>
      <span>Status</span>
      <span>Incident</span>
      <span>Detected by</span>
      <span className="text-right">Resolved in</span>
      <span className="text-right">Cost</span>
      <span className="text-right">Opened</span>
    </div>
  );
}

/** Incident rows: status, title with severity and resources, detector, time to resolve, cost, when. */
export function IncidentList({ incidents, now = Date.now() }: { incidents: IncidentSummary[]; now?: number }) {
  return (
    <ul className="divide-y divide-border">
      {incidents.map((i) => (
        <li key={i.id}>
          <Link
            href={`/incidents/${i.id}`}
            className={cn(
              "grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-4 gap-y-1 px-4 py-2.5 hover:bg-accent/60 focus-visible:bg-accent/60",
              GRID,
            )}
          >
            <span>
              <IncidentStatusPill status={i.status} />
            </span>
            <span className="min-w-0">
              <span className="block truncate" title={i.title}>{i.title}</span>
              <span className="flex flex-wrap items-center gap-x-2 text-[12px] text-muted-foreground">
                <SeverityTag severity={i.severity} />
                <span className="truncate">{i.affected_resources.map((r) => r.name).join(", ")}</span>
                {i.memory_reused && <span className="text-subtle">seen before</span>}
              </span>
            </span>
            <span className="hidden min-w-0 truncate text-[12.5px] text-muted-foreground sm:block">
              {i.detected_by === "user" ? "A person" : ruleLabel(i.detector_rule)}
            </span>
            <span className="tabular hidden text-right text-[12px] text-muted-foreground sm:block">
              {i.duration_ms !== null ? formatDuration(i.duration_ms) : ""}
            </span>
            <span className="tabular hidden text-right font-mono text-[12px] text-muted-foreground sm:block">
              {formatCost(i.cost_usd) ?? ""}
            </span>
            <time dateTime={i.opened_at} title={formatTimestamp(i.opened_at)} className="text-right text-[12px] text-muted-foreground">
              {formatRelative(i.opened_at, now)}
            </time>
          </Link>
        </li>
      ))}
    </ul>
  );
}
