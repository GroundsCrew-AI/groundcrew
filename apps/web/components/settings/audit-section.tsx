"use client";

import { ErrorState, RowsSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { formatRelative, formatTimestamp } from "@/lib/format";
import { auditSummary, useAuditLog } from "@/lib/workspace-admin";

/** Who approved what and when, and every change to members, connections, and settings. */
export function AuditSection({ workspaceId }: { workspaceId: string }) {
  const log = useAuditLog(workspaceId);
  if (log.error) return <ErrorState error={log.error} onRetry={() => void log.refetch()} />;
  if (!log.data) {
    return (
      <div className="overflow-hidden rounded-lg border">
        <RowsSkeleton rows={3} />
      </div>
    );
  }
  const events = log.data.pages.flatMap((p) => p.items);
  if (events.length === 0) return <p className="text-[12.5px] text-muted-foreground">Nothing yet.</p>;
  return (
    <div className="space-y-3">
      <ul className="divide-y overflow-hidden rounded-lg border">
        {events.map((e) => (
          <li key={e.id} className="flex flex-wrap items-baseline gap-x-2 px-4 py-2.5 text-[12.5px]">
            <span className="font-medium">{e.actor}</span>
            <span className="min-w-0 flex-1 break-words text-muted-foreground">{auditSummary(e)}</span>
            <time dateTime={e.created_at} title={formatTimestamp(e.created_at)} className="text-subtle">
              {formatRelative(e.created_at)}
            </time>
          </li>
        ))}
      </ul>
      {log.hasNextPage && (
        <Button size="sm" variant="outline" disabled={log.isFetchingNextPage} onClick={() => void log.fetchNextPage()}>
          {log.isFetchingNextPage ? "Loading" : "Show older"}
        </Button>
      )}
    </div>
  );
}
