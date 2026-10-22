"use client";

import { Coins, type LucideIcon } from "lucide-react";
import Link from "next/link";
import type { CSSProperties, ReactNode } from "react";

import { GettingStarted } from "@/components/overview/getting-started";
import { formatUsd, SpendMeter } from "@/components/overview/usage";
import { RunList } from "@/components/runs/run-list";
import { PageBody, PageHeader } from "@/components/shell/page-header";
import { RunsIllustration } from "@/components/states/illustrations";
import { EmptyState, ErrorState, RowsSkeleton } from "@/components/states/states";
import { Skeleton } from "@/components/ui/skeleton";
import type { Summary } from "@/lib/api/client";
import { AREAS, areaColor, type AreaKey } from "@/lib/areas";
import { formatTokens } from "@/lib/format";
import { useRuns, useSummary } from "@/lib/queries";
import { useWorkspace } from "@/lib/workspace";

function Metric({
  label,
  value,
  children,
  href,
  area,
  icon,
}: {
  label: string;
  value: ReactNode;
  children?: ReactNode;
  href?: string;
  /** The area it belongs to: its icon and hue. */
  area: AreaKey;
  /** Another icon in the area's hue. */
  icon?: LucideIcon;
}) {
  const Icon = icon ?? AREAS.find((a) => a.key === area)!.icon;
  const body = (
    <>
      <p className="flex items-center justify-between gap-2 text-[12px] text-muted-foreground">
        {label}
        <span
          aria-hidden="true"
          className="area-chip flex size-6 shrink-0 items-center justify-center rounded-md"
          style={{ "--area": areaColor(area) } as CSSProperties}
        >
          <Icon className="size-3.5" />
        </span>
      </p>
      <p className="tabular mt-0.5 text-[22px] leading-none font-semibold tracking-tight">{value}</p>
      {children && <div className="mt-2.5">{children}</div>}
    </>
  );
  const className = "block min-w-0 bg-card px-4 py-4 sm:px-5";
  return href ? (
    <Link href={href} className={`${className} hover:bg-accent/50 focus-visible:bg-accent/50`}>
      {body}
    </Link>
  ) : (
    <div className={className}>{body}</div>
  );
}

function Note({ children }: { children: ReactNode }) {
  return <p className="tabular text-[12px] text-muted-foreground">{children}</p>;
}

function Metrics({ summary }: { summary: Summary }) {
  const incidents = summary.open_incidents ?? 0;
  const approvals = summary.pending_approvals;
  const fixes = summary.pending_permanent_fixes ?? 0;
  return (
    <>
      <Metric
        label="Open incidents"
        href="/incidents"
        area="incidents"
        value={<span className={incidents ? "text-[var(--status-failed)]" : undefined}>{incidents}</span>}
      >
        <Note>
          {incidents
            ? "View incidents"
            : fixes
              ? `None open. ${fixes} with a permanent fix pending`
              : "Nothing open right now"}
        </Note>
      </Metric>
      <Metric
        label="Waiting for approval"
        href="/approvals"
        area="approvals"
        value={<span className={approvals ? "text-[var(--status-waiting)]" : undefined}>{approvals}</span>}
      >
        <Note>{approvals ? "Agents wait until someone decides" : "Nothing waiting"}</Note>
      </Metric>
      <Metric label="Model spend today" area="overview" icon={Coins} value={formatUsd(summary.usd_today)}>
        <SpendMeter summary={summary} />
      </Metric>
      <Metric label="Runs today" href="/runs" area="runs" value={summary.runs_today}>
        <Note>{formatTokens(summary.workspace_tokens_today)} tokens today</Note>
      </Metric>
    </>
  );
}

export function OverviewHome() {
  const { workspace, error: workspaceError, refetch } = useWorkspace();
  const summary = useSummary(workspace?.id);
  const runs = useRuns(workspace?.id, 6);
  const recent = runs.data?.pages[0]?.items ?? [];
  const fixes = summary.data?.pending_permanent_fixes ?? 0;

  return (
    <>
      <PageHeader title="Overview" />
      <PageBody className="max-w-5xl space-y-8">
        <GettingStarted />

        {workspaceError || summary.error ? (
          <ErrorState
            error={workspaceError ?? summary.error}
            onRetry={() => (workspaceError ? refetch() : void summary.refetch())}
          />
        ) : (
          <section
            aria-label="At a glance"
            className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border bg-border lg:grid-cols-4"
          >
            {summary.data ? (
              <Metrics summary={summary.data} />
            ) : (
              [0, 1, 2, 3].map((i) => (
                <div key={i} className="space-y-2 bg-card px-4 py-4 sm:px-5">
                  <Skeleton className="h-3 w-24" />
                  <Skeleton className="h-6 w-12" />
                  <Skeleton className="h-3 w-32" />
                </div>
              ))
            )}
          </section>
        )}

        {fixes > 0 && (
          <Link
            href="/incidents?pending_fix=true"
            className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 rounded-lg border border-[color-mix(in_oklab,var(--status-waiting)_45%,var(--border))] px-4 py-2.5 text-[13px] hover:bg-accent/60"
          >
            <span>
              <span className="tabular font-medium text-[var(--status-waiting)]">{fixes}</span>{" "}
              {fixes === 1 ? "incident has" : "incidents have"} a permanent fix pending
            </span>
            <span className="text-[12.5px] text-muted-foreground">Resolved by a rollback; the fix PR is not deployed</span>
          </Link>
        )}

        <section aria-labelledby="recent-runs">
          <div className="mb-2 flex items-center justify-between">
            <h2 id="recent-runs" className="text-[13px] font-medium">
              Recent activity
            </h2>
            {recent.length > 0 && (
              <Link href="/runs" className="text-[12.5px] text-muted-foreground hover:text-foreground">
                All runs
              </Link>
            )}
          </div>
          <div className="rounded-lg border">
            {runs.error ? (
              <div className="p-4">
                <ErrorState error={runs.error} onRetry={() => void runs.refetch()} />
              </div>
            ) : runs.isLoading || !workspace ? (
              <RowsSkeleton rows={4} />
            ) : recent.length === 0 ? (
              <div className="px-5">
                <EmptyState illustration={<RunsIllustration />} title="Nothing has run yet">
                  Discovery, deploys, and Medic&apos;s incident investigations show up here step by step while
                  they run.
                </EmptyState>
              </div>
            ) : (
              <RunList runs={recent} />
            )}
          </div>
        </section>
      </PageBody>
    </>
  );
}
