"use client";

import { ErrorState, RowsSkeleton } from "@/components/states/states";
import { formatCost, formatTimestamp } from "@/lib/format";
import { type Usage, useUsage } from "@/lib/workspace-admin";

function Meter({ label, used, limit, note }: { label: string; used: number; limit: number; note?: string }) {
  const share = limit > 0 ? Math.min(1, used / limit) : 1;
  const full = used >= limit;
  return (
    <li className="space-y-1.5 px-4 py-3">
      <div className="flex items-baseline justify-between gap-3 text-[12.5px]">
        <span>{label}</span>
        <span className={full ? "font-medium text-[var(--status-failed)]" : "text-muted-foreground"}>
          {note ?? `${used} of ${limit}`}
        </span>
      </div>
      <div
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={limit}
        aria-valuenow={Math.min(used, limit)}
        className="h-1.5 overflow-hidden rounded-full bg-muted"
      >
        <div
          className={full ? "h-full bg-[var(--status-failed)]" : "h-full bg-foreground/60"}
          style={{ width: `${share * 100}%` }}
        />
      </div>
    </li>
  );
}

export function UsageList({ usage }: { usage: Usage }) {
  return (
    <div className="space-y-3">
      {usage.platform_paused && (
        <p role="alert" className="rounded-lg border px-3 py-2.5 text-[12.5px] text-[var(--status-failed)]">
          Groundcrew reached its model spend limit for today, so agents are paused for every workspace. It resets at
          00:00 UTC.
        </p>
      )}
      <ul className="divide-y overflow-hidden rounded-lg border">
        <Meter
          label="Model spend today"
          used={usage.usd_today}
          limit={usage.daily_usd_cap}
          note={`${formatCost(usage.usd_today)} of ${formatCost(usage.daily_usd_cap)}`}
        />
        {usage.quotas.map((q) => (
          <Meter key={q.name} label={q.label} used={q.used} limit={q.limit} />
        ))}
      </ul>
      <p className="text-[12px] text-muted-foreground">
        Daily counts reset at <time dateTime={usage.resets_at}>{formatTimestamp(usage.resets_at)}</time>. One build
        attempt may take up to {Math.round(usage.max_build_seconds / 60)} minutes.
      </p>
    </div>
  );
}

/** Today's use of this workspace's limits. */
export function UsageSection({ workspaceId }: { workspaceId: string }) {
  const usage = useUsage(workspaceId);
  if (usage.error) return <ErrorState error={usage.error} onRetry={() => void usage.refetch()} />;
  if (!usage.data) {
    return (
      <div className="overflow-hidden rounded-lg border">
        <RowsSkeleton rows={3} />
      </div>
    );
  }
  return <UsageList usage={usage.data} />;
}
