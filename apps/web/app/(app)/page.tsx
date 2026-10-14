"use client";

import Link from "next/link";

import { NewRunDialog } from "@/components/runs/new-run-dialog";
import { RunList } from "@/components/runs/run-list";
import { PageBody, PageHeader } from "@/components/shell/page-header";
import { RunsIllustration } from "@/components/states/illustrations";
import { EmptyState, ErrorState, RowsSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import type { Summary } from "@/lib/api/client";
import { formatTokens } from "@/lib/format";
import { useRuns, useSummary } from "@/lib/queries";
import { useWorkspace } from "@/lib/workspace";

function Metric({
  label,
  value,
  children,
  href,
}: {
  label: string;
  value: React.ReactNode;
  children?: React.ReactNode;
  href?: string;
}) {
  const body = (
    <>
      <p className="text-[12px] text-muted-foreground">{label}</p>
      <p className="tabular mt-1 text-[22px] leading-none font-semibold tracking-tight">{value}</p>
      {children && <div className="mt-2.5">{children}</div>}
    </>
  );
  const className = "block min-w-0 px-5 py-4";
  return href ? (
    <Link href={href} className={`${className} hover:bg-accent/50`}>
      {body}
    </Link>
  ) : (
    <div className={className}>{body}</div>
  );
}

function BudgetMeter({ summary }: { summary: Summary }) {
  const budget = summary.daily_token_budget;
  if (!budget) {
    return <p className="text-[12px] text-muted-foreground">No daily budget set (DAILY_TOKEN_BUDGET).</p>;
  }
  const used = summary.budget_tokens_today;
  const percent = Math.min(100, Math.round((used / budget) * 100));
  const tone = percent >= 90 ? "var(--status-failed)" : percent >= 70 ? "var(--status-waiting)" : "var(--primary)";
  return (
    <div className="space-y-1.5">
      <Progress
        value={percent}
        aria-label={`${percent}% of the daily token budget used`}
        className="h-1"
        style={{ "--progress-color": tone } as React.CSSProperties}
      />
      <p className="tabular text-[12px] text-muted-foreground">
        {formatTokens(used)} of {formatTokens(budget)} shared daily budget
      </p>
    </div>
  );
}

export default function OverviewPage() {
  const { workspace, error: workspaceError, refetch } = useWorkspace();
  const summary = useSummary(workspace?.id);
  const runs = useRuns(workspace?.id, 6);
  const recent = runs.data?.pages[0]?.items ?? [];

  return (
    <>
      <PageHeader title="Overview" actions={<NewRunDialog />} />
      <PageBody className="max-w-5xl space-y-8">
        {workspaceError || summary.error ? (
          <ErrorState
            error={workspaceError ?? summary.error}
            onRetry={() => (workspaceError ? refetch() : void summary.refetch())}
          />
        ) : (
          <section
            aria-label="Today"
            className="grid divide-y rounded-lg border bg-card sm:grid-cols-3 sm:divide-x sm:divide-y-0"
          >
            {summary.data ? (
              <>
                <Metric label="Runs today" value={summary.data.runs_today} href="/runs" />
                <Metric
                  label="Waiting for approval"
                  value={
                    <span className={summary.data.pending_approvals ? "text-[var(--status-waiting)]" : undefined}>
                      {summary.data.pending_approvals}
                    </span>
                  }
                  href="/approvals"
                />
                <Metric label="Tokens today" value={formatTokens(summary.data.workspace_tokens_today)}>
                  <BudgetMeter summary={summary.data} />
                </Metric>
              </>
            ) : (
              [0, 1, 2].map((i) => (
                <div key={i} className="space-y-2 px-5 py-4">
                  <Skeleton className="h-3 w-24" />
                  <Skeleton className="h-6 w-12" />
                </div>
              ))
            )}
          </section>
        )}

        {(summary.data?.pending_permanent_fixes ?? 0) > 0 && (
          <Link
            href="/incidents?pending_fix=true"
            className="flex items-center justify-between rounded-lg border border-[color-mix(in_oklab,var(--status-waiting)_45%,var(--border))] px-4 py-2.5 text-[13px] hover:bg-accent/60"
          >
            <span>
              <span className="tabular font-medium text-[var(--status-waiting)]">
                {summary.data?.pending_permanent_fixes}
              </span>{" "}
              {summary.data?.pending_permanent_fixes === 1 ? "incident has" : "incidents have"} a permanent fix
              pending
            </span>
            <span className="text-[12.5px] text-muted-foreground">Resolved by a rollback; the fix PR is not deployed</span>
          </Link>
        )}

        <section aria-labelledby="recent-runs">
          <div className="mb-2 flex items-center justify-between">
            <h2 id="recent-runs" className="text-[13px] font-medium">
              Recent runs
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
                <EmptyState
                  illustration={<RunsIllustration />}
                  title="Nothing has run yet"
                  action={<NewRunDialog trigger={<Button size="sm">Start a test run</Button>} />}
                >
                  Start Echo Ops to see a live run: it lists services, asks to restart one, and waits for you.
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
