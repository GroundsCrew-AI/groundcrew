"use client";

import { cn } from "cn";
import Link from "next/link";
import type { CSSProperties, ReactNode } from "react";

import { DeployStatePill } from "@/components/deploy/deploy-list";
import { Skeleton } from "@/components/ui/skeleton";
import type { Activity, Summary } from "@/lib/api/client";
import { useDeploys } from "@/lib/deploys";
import { type Connection, useConnections } from "@/lib/discovery";
import { agentName, formatCost, formatDuration, formatRelative, formatTokens } from "@/lib/format";

import { ColumnChart, type Series } from "./column-chart";
import { formatUsd } from "./usage";

/**
 * The overview's panels: activity and spend over the last days, how each agent is doing,
 * incidents, recent deploys, and the connections everything comes from.
 */

/** Agents with their own series color; any other agent is "Other". Color follows the agent. */
const AGENT_SERIES = [
  { key: "discovery", label: "Discovery", color: "var(--series-1)" },
  { key: "deploy", label: "Deploy", color: "var(--series-2)" },
  { key: "medic", label: "Medic", color: "var(--series-3)" },
] as const;
const OTHER = { key: "other", label: "Other", color: "var(--series-4)" };

export function agentColor(agent: string): string {
  return AGENT_SERIES.find((s) => s.key === agent)?.color ?? OTHER.color;
}

export function runSeries(activity: Activity): Series[] {
  const known = new Set<string>(AGENT_SERIES.map((s) => s.key));
  const series: Series[] = AGENT_SERIES.map((s) => ({
    ...s,
    values: activity.days.map((d) => d.runs[s.key] ?? 0),
  }));
  const other = activity.days.map((d) =>
    Object.entries(d.runs).reduce((sum, [agent, n]) => (known.has(agent) ? sum : sum + n), 0),
  );
  if (other.some(Boolean)) series.push({ ...OTHER, values: other });
  // Agents with no runs in the window stay out of the legend.
  return series.filter((s) => s.values.some(Boolean));
}

export function Panel({
  title,
  description,
  action,
  className,
  children,
}: {
  title: string;
  description?: ReactNode;
  action?: { href: string; label: string };
  className?: string;
  children: ReactNode;
}) {
  const id = title.toLowerCase().replace(/\W+/g, "-");
  return (
    <section aria-labelledby={id} className={cn("min-w-0 rounded-lg border bg-card p-4 sm:p-5", className)}>
      <div className="mb-4 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 id={id} className="text-[13px] font-medium">{title}</h2>
          {description && <p className="mt-0.5 text-[12px] text-muted-foreground">{description}</p>}
        </div>
        {action && (
          <Link href={action.href} className="shrink-0 text-[12.5px] text-muted-foreground hover:text-foreground">
            {action.label}
          </Link>
        )}
      </div>
      {children}
    </section>
  );
}

function PanelSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-2.5">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-4 w-full" />
      ))}
    </div>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center text-[12.5px] text-muted-foreground">{children}</p>;
}

function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="min-w-0">
      <p className="text-[12px] text-muted-foreground">{label}</p>
      <p className="tabular mt-0.5 text-[17px] leading-tight font-semibold tracking-tight">{value}</p>
      {hint && <p className="tabular mt-0.5 text-[11.5px] text-subtle">{hint}</p>}
    </div>
  );
}

function percentOf(part: number, whole: number): string {
  return whole ? `${Math.round((part / whole) * 100)}%` : "–";
}

// --- activity and spend ------------------------------------------------------------------

export function RunsPanel({ activity, className }: { activity: Activity | undefined; className?: string }) {
  const days = activity?.days ?? [];
  const runs = days.reduce((sum, d) => sum + Object.values(d.runs).reduce((a, b) => a + b, 0), 0);
  const succeeded = days.reduce((sum, d) => sum + d.succeeded, 0);
  const failed = days.reduce((sum, d) => sum + d.failed, 0);
  return (
    <Panel
      title="Agent activity"
      description={`Runs started per day, last ${days.length || 14} days (UTC)`}
      action={{ href: "/runs", label: "All runs" }}
      className={className}
    >
      {!activity ? (
        <PanelSkeleton rows={6} />
      ) : runs === 0 ? (
        <Empty>No runs in the last {days.length} days.</Empty>
      ) : (
        <div className="space-y-4">
          <div className="grid grid-cols-3 gap-4">
            <Stat label="Runs" value={runs} />
            <Stat label="Succeeded" value={percentOf(succeeded, succeeded + failed)} hint={`${succeeded} of ${succeeded + failed} finished`} />
            <Stat label="Failed or stopped" value={failed} />
          </div>
          <ColumnChart label="Runs per day by agent" days={days.map((d) => d.day)} series={runSeries(activity)} />
        </div>
      )}
    </Panel>
  );
}

export function SpendPanel({
  activity,
  summary,
  className,
}: {
  activity: Activity | undefined;
  summary: Summary | undefined;
  className?: string;
}) {
  const days = activity?.days ?? [];
  const usd = days.reduce((sum, d) => sum + d.usd, 0);
  const tokens = days.reduce((sum, d) => sum + d.tokens, 0);
  const busiest = days.reduce<(typeof days)[number] | null>((best, d) => (!best || d.usd > best.usd ? d : best), null);
  const cap = summary?.daily_usd_cap ?? null;
  return (
    <Panel
      title="Model spend"
      description={`USD per day, last ${days.length || 14} days`}
      action={{ href: "/settings", label: "Limits" }}
      className={className}
    >
      {!activity ? (
        <PanelSkeleton rows={6} />
      ) : usd === 0 ? (
        <Empty>No model spend in the last {days.length} days.</Empty>
      ) : (
        <div className="space-y-4">
          <div className="grid grid-cols-3 gap-4">
            <Stat label="In this period" value={formatUsd(usd)} />
            <Stat label="Tokens" value={formatTokens(tokens)} />
            <Stat label="Daily average" value={formatUsd(days.length ? usd / days.length : 0)} hint={busiest && busiest.usd > 0 ? `Most: ${formatUsd(busiest.usd)}` : undefined} />
          </div>
          <ColumnChart
            label="Model spend per day in USD"
            days={days.map((d) => d.day)}
            series={[{ key: "usd", label: "Spend", color: "var(--series-4)", values: days.map((d) => d.usd) }]}
            format={(v) => formatCost(v) ?? "$0"}
            // The cap only fits the axis when spend gets near it; otherwise it would flatten the bars.
            reference={cap !== null && cap <= Math.max(...days.map((d) => d.usd), 0) * 4 ? { value: cap, label: `Daily cap ${formatUsd(cap)}` } : undefined}
          />
        </div>
      )}
    </Panel>
  );
}

// --- agents ------------------------------------------------------------------------------

export function AgentsPanel({ activity, className }: { activity: Activity | undefined; className?: string }) {
  const agents = activity?.agents ?? [];
  return (
    <Panel title="Agents" description="Last 14 days" className={className}>
      {!activity ? (
        <PanelSkeleton />
      ) : agents.length === 0 ? (
        <Empty>No agent has run yet.</Empty>
      ) : (
        <ul className="space-y-3.5">
          {agents.map((a) => {
            const finished = a.succeeded + a.failed;
            const rate = finished ? a.succeeded / finished : null;
            return (
              <li key={a.agent} className="space-y-1.5">
                <div className="flex items-baseline justify-between gap-3 text-[13px]">
                  <span className="inline-flex min-w-0 items-center gap-2">
                    <span aria-hidden="true" className="size-2 shrink-0 rounded-[2px]" style={{ background: agentColor(a.agent) }} />
                    <span className="truncate font-medium">{agentName(a.agent)}</span>
                  </span>
                  <span className="tabular shrink-0 text-[12px] text-muted-foreground">
                    {a.runs} {a.runs === 1 ? "run" : "runs"}
                    {a.median_seconds !== null && a.median_seconds !== undefined && ` · ${formatDuration(a.median_seconds * 1000)} typical`}
                  </span>
                </div>
                <div
                  role="meter"
                  aria-label={`${agentName(a.agent)} success rate`}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={rate === null ? 0 : Math.round(rate * 100)}
                  aria-valuetext={rate === null ? "No finished runs" : `${Math.round(rate * 100)}% succeeded`}
                  className="flex h-1.5 gap-[2px] overflow-hidden rounded-full bg-muted"
                >
                  {a.succeeded > 0 && <div className="bg-[var(--status-ok)]" style={{ flexGrow: a.succeeded }} />}
                  {a.failed > 0 && <div className="bg-[var(--status-failed)]" style={{ flexGrow: a.failed }} />}
                </div>
                <p className="tabular text-[11.5px] text-subtle">
                  {rate === null ? "None finished yet" : `${Math.round(rate * 100)}% succeeded`}
                  {a.failed > 0 && `, ${a.failed} failed or stopped`} · {formatTokens(a.tokens)} tokens
                </p>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

// --- incidents ---------------------------------------------------------------------------

const SEVERITIES: { key: string; label: string; color: string }[] = [
  { key: "critical", label: "Critical", color: "var(--status-failed)" },
  { key: "high", label: "High", color: "var(--status-failed)" },
  { key: "medium", label: "Medium", color: "var(--status-waiting)" },
  { key: "low", label: "Low", color: "var(--status-neutral)" },
  { key: "unknown", label: "Not rated", color: "var(--status-neutral)" },
];

export function IncidentsPanel({ activity, className }: { activity: Activity | undefined; className?: string }) {
  const days = activity?.days ?? [];
  const opened = days.reduce((sum, d) => sum + d.incidents_opened, 0);
  const resolved = days.reduce((sum, d) => sum + d.incidents_resolved, 0);
  const open = activity?.open_by_severity ?? {};
  const openTotal = Object.values(open).reduce((a, b) => a + b, 0);
  const mttr = activity?.median_resolve_seconds;
  return (
    <Panel title="Incidents" description="Last 14 days" action={{ href: "/incidents", label: "All incidents" }} className={className}>
      {!activity ? (
        <PanelSkeleton />
      ) : (
        <div className="space-y-4">
          <div className="grid grid-cols-3 gap-4">
            <Stat label="Opened" value={opened} />
            <Stat label="Resolved" value={resolved} />
            <Stat label="Typical fix" value={mttr === null || mttr === undefined ? "–" : formatDuration(mttr * 1000)} hint="open to resolved" />
          </div>
          <div>
            <p className="mb-2 text-[12px] text-muted-foreground">Open now by severity</p>
            {openTotal === 0 ? (
              <p className="text-[12.5px] text-muted-foreground">Nothing open. Medic opens one when a watched service fails.</p>
            ) : (
              <ul className="flex flex-wrap gap-2">
                {SEVERITIES.filter((s) => open[s.key]).map((s) => (
                  <li key={s.key}>
                    <Link
                      href="/incidents"
                      className="tint inline-flex h-6 items-center gap-1.5 rounded-full px-2.5 text-[12px] font-medium"
                      style={{ "--tint": s.color } as CSSProperties}
                    >
                      {s.label}{" "}
                      <span className="tabular">{open[s.key]}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </Panel>
  );
}

// --- deploys and connections ---------------------------------------------------------------

export function DeploysPanel({ workspaceId, className }: { workspaceId: string | undefined; className?: string }) {
  const deploys = useDeploys(workspaceId);
  const items = deploys.data?.items ?? [];
  const live = items.filter((d) => d.status === "live").length;
  return (
    <Panel
      title="Recent deploys"
      description={items.length ? `${live} live of the last ${items.length}` : undefined}
      action={{ href: "/deploy", label: "Deploy" }}
      className={className}
    >
      {deploys.isLoading || !workspaceId ? (
        <PanelSkeleton />
      ) : items.length === 0 ? (
        <Empty>
          No deploys yet.{" "}
          <Link href="/deploy" className="text-foreground underline underline-offset-4">Deploy a repository</Link>
        </Empty>
      ) : (
        <ul className="-mx-2 divide-y divide-border">
          {items.slice(0, 5).map((d) => (
            <li key={d.id}>
              <Link href={`/deploy/${d.id}`} className="flex items-center gap-3 rounded-md px-2 py-2 hover:bg-accent/50">
                <DeployStatePill state={d.status} className="w-[112px] shrink-0 justify-start" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13px] font-medium">{d.app ?? d.source.repo}</span>
                  {d.target.name && (
                    <span className="block truncate text-[12px] text-muted-foreground">to {d.target.name}</span>
                  )}
                </span>
                <span className="tabular shrink-0 text-[12px] text-subtle">{formatRelative(d.created_at)}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

const KIND_LABEL: Record<Connection["kind"], string> = {
  aws: "AWS",
  github: "GitHub",
  vercel: "Vercel",
  azure: "Azure",
  k8s: "Kubernetes",
  agent: "Server agent",
};

const CONNECTION_STATUS: Record<Connection["status"], { label: string; color: string }> = {
  connected: { label: "Connected", color: "var(--status-ok)" },
  pending: { label: "Pending", color: "var(--status-waiting)" },
  error: { label: "Error", color: "var(--status-failed)" },
};

export function ConnectionsPanel({ workspaceId, className }: { workspaceId: string | undefined; className?: string }) {
  const connections = useConnections(workspaceId);
  const items = connections.data ?? [];
  const healthy = items.filter((c) => c.status === "connected").length;
  const resources = items.reduce((sum, c) => sum + (c.last_discovery?.resources ?? 0), 0);
  return (
    <Panel
      title="Connections"
      description={items.length ? `${healthy} of ${items.length} connected · ${resources} resources found` : undefined}
      action={{ href: "/map", label: "Map" }}
      className={className}
    >
      {connections.isLoading || !workspaceId ? (
        <PanelSkeleton />
      ) : items.length === 0 ? (
        <Empty>
          Nothing connected yet.{" "}
          <Link href="/connections" className="text-foreground underline underline-offset-4">Add a connection</Link>
        </Empty>
      ) : (
        <ul className="-mx-2 divide-y divide-border">
          {items.slice(0, 6).map((c) => {
            const status = CONNECTION_STATUS[c.status];
            const found = c.last_discovery;
            return (
              <li key={c.id} className="flex items-center gap-3 px-2 py-2">
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13px] font-medium">{c.name}</span>
                  <span className="block truncate text-[12px] text-muted-foreground">
                    {KIND_LABEL[c.kind]}
                    {found?.resources !== null && found?.resources !== undefined && ` · ${found.resources} resources`}
                    {found && ` · checked ${formatRelative(found.at)}`}
                  </span>
                </span>
                <span
                  className="tint inline-flex h-5 shrink-0 items-center gap-1.5 rounded-full px-2 text-[12px] font-medium"
                  style={{ "--tint": status.color } as CSSProperties}
                >
                  <span aria-hidden="true" className="size-1.5 rounded-full bg-current" />
                  {status.label}
                </span>
              </li>
            );
          })}
          {items.length > 6 && (
            <li className="px-2 pt-2 text-[12px]">
              <Link href="/connections" className="text-muted-foreground hover:text-foreground">
                {items.length - 6} more
              </Link>
            </li>
          )}
        </ul>
      )}
    </Panel>
  );
}
