"use client";

import { GitPullRequest, RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { canRetryFix, type IncidentDetail } from "@/lib/incidents";

/**
 * The incident's fixes and where each stands. A failed emergency fix shows why it failed and,
 * once its cause is fixed, a Retry that plans it again and asks for a new approval.
 */
export function Fixes({
  incident,
  onRetry,
  retrying,
  retryError,
}: {
  incident: IncidentDetail;
  onRetry: (fixId: string) => void;
  retrying: string | null;
  /** Why the last retry could not be planned, by fix id (e.g. still no PassRole). */
  retryError: { fixId: string; message: string } | null;
}) {
  if (incident.fixes.length === 0) return null;
  return (
    <section aria-label="Fixes" className="space-y-2">
      <h2 className="text-[13px] font-medium">Fixes</h2>
      <ol className="divide-y rounded-lg border">
        {incident.fixes.map((f) => (
          <li key={f.fix.fix_id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-[13px]">
            <span className="text-subtle">{f.fix.order}.</span>
            <span className="min-w-0 flex-1">{f.fix.title}</span>
            <span className="text-[12px] text-muted-foreground">{f.fix.kind === "pr" ? "Pull request" : "Emergency action"}</span>
            <span className="text-[12px] font-medium capitalize">{f.status.replaceAll("_", " ")}</span>
            {f.pull_request && (
              <a href={f.pull_request.url} target="_blank" rel="noreferrer"
                className="inline-flex items-center gap-1 font-mono text-[12px] underline-offset-4 hover:underline">
                <GitPullRequest aria-hidden="true" className="size-3.5" />#{f.pull_request.number} {f.pull_request.state}
                <span className="sr-only">(opens in a new tab)</span>
              </a>
            )}
            {canRetryFix(incident, f) && (
              <Button size="sm" variant="outline" disabled={retrying !== null} onClick={() => onRetry(f.fix.fix_id)}>
                <RotateCcw aria-hidden="true" />
                {retrying === f.fix.fix_id ? "Planning" : "Retry"}
              </Button>
            )}
            {f.applied?.ok && f.applied.action_result?.details?.skipped && (
              <p className="basis-full text-[12.5px] text-muted-foreground">{f.applied.action_result.summary}</p>
            )}
            {f.status === "failed" && f.applied?.error && (
              <p className="basis-full text-[12.5px] [overflow-wrap:anywhere] text-[var(--status-failed)]">
                Failed: {f.applied.error}
              </p>
            )}
            {canRetryFix(incident, f) && (
              <p className="basis-full text-[12px] text-muted-foreground">
                Fix the cause first (for a permission error, update the connection&apos;s access). Retry plans it again
                and asks for a new approval; nothing changes until someone approves.
              </p>
            )}
            {retryError?.fixId === f.fix.fix_id && (
              <p role="alert" className="basis-full text-[12.5px] [overflow-wrap:anywhere] text-[var(--status-failed)]">
                {retryError.message}
              </p>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}
