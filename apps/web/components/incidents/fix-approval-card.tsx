"use client";

import { cn } from "cn";
import { ArrowRight, Check, X } from "lucide-react";

import { DecidedBy } from "@/components/runs/approval-card";
import { PolicyTag } from "@/components/runs/badges";
import { Button } from "@/components/ui/button";
import type { Approval, Decision } from "@/lib/api/client";
import { formatRelative, formatTimestamp } from "@/lib/format";
import type { ProposedFix } from "@/lib/incidents";

/** One line of a unified diff, coloured like a code review. */
function DiffLine({ line }: { line: string }) {
  const added = line.startsWith("+") && !line.startsWith("+++");
  const removed = line.startsWith("-") && !line.startsWith("---");
  return (
    <span
      className={cn(
        "block whitespace-pre-wrap break-words",
        added && "bg-[color-mix(in_oklab,var(--status-ok)_14%,transparent)]",
        removed && "bg-[color-mix(in_oklab,var(--status-failed)_14%,transparent)]",
        !added && !removed && "text-muted-foreground",
      )}
    >
      {line || " "}
    </span>
  );
}

/**
 * The approval card for a Medic fix: an emergency action as "what: before -> after" lines, or
 * a pull request as its diff. Approving runs exactly this, once.
 */
export function FixApprovalCard({
  fix,
  approval,
  onDecide,
  deciding,
  runEnded = false,
  waitingFor = null,
}: {
  fix: ProposedFix;
  /** The prerequisite fix that must be applied before this one can be approved. */
  waitingFor?: string | null;
  approval: Approval;
  onDecide: (decision: Decision) => void;
  deciding: Decision | null;
  runEnded?: boolean;
}) {
  const pending = approval.status === "pending";
  const open = pending && !runEnded;
  const pr = fix.pull_request;
  return (
    <section
      aria-label={`Approval for ${fix.title}`}
      className={cn(
        "rounded-lg border bg-card",
        open && "border-[color-mix(in_oklab,var(--status-waiting)_45%,var(--border))]",
      )}
    >
      <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
        <span className="text-[13px] font-medium">
          {open ? "Approval needed" : pending ? "Not decided" : "Approval"}
        </span>
        <span className="text-[12.5px] text-muted-foreground">
          Fix {fix.order}: {fix.kind === "emergency" ? "emergency action" : "pull request"}
        </span>
        <PolicyTag policy={approval.policy} />
        <time
          dateTime={approval.created_at}
          title={formatTimestamp(approval.created_at)}
          className="ml-auto text-[12px] text-muted-foreground"
        >
          Requested {formatRelative(approval.created_at)}
        </time>
      </div>
      <div className="space-y-3 px-3 py-3">
        <div>
          <p className="text-[13.5px] font-medium">{fix.title}</p>
          <p className="mt-0.5 text-[12.5px] text-muted-foreground">{fix.rationale}</p>
          {fix.degraded && (
            <p className="mt-1.5 text-[12.5px]">
              <span className="mr-1.5 rounded border border-[color-mix(in_oklab,var(--status-waiting)_45%,var(--border))] px-1 text-[11px] font-medium text-[var(--status-waiting)]">
                Service degraded, not down
              </span>
              <span className="text-muted-foreground">{fix.degraded}</span>
            </p>
          )}
        </div>
        {(fix.plan_lines ?? []).length > 0 && (
          <ul className="space-y-1 rounded-md border bg-background px-3 py-2 text-[12.5px]">
            {(fix.plan_lines ?? []).map((line) => (
              <li key={line.what} className="flex flex-wrap items-center gap-x-2">
                <span className="text-muted-foreground">{line.what}</span>
                <span className="font-mono">{line.before ?? "-"}</span>
                <ArrowRight aria-label="becomes" className="size-3 text-subtle" />
                <span className="font-mono">{line.after ?? "-"}</span>
              </li>
            ))}
          </ul>
        )}
        {pr && (
          <div className="space-y-2">
            <p className="text-[12.5px] text-muted-foreground">
              Opens a pull request on <span className="font-mono text-foreground">{pr.repo}</span> from{" "}
              <span className="font-mono text-foreground">{pr.branch}</span> into{" "}
              <span className="font-mono text-foreground">{pr.base}</span>. It is not merged.
            </p>
            {pr.files.map((file) => (
              <figure key={file.path} className="overflow-hidden rounded-md border">
                <figcaption className="border-b bg-muted px-3 py-1 font-mono text-[12px]">{file.path}</figcaption>
                <pre className="max-h-80 overflow-auto bg-background px-3 py-2 font-mono text-[12px] leading-relaxed">
                  {file.diff.split("\n").map((line, i) => <DiffLine key={i} line={line} />)}
                </pre>
              </figure>
            ))}
          </div>
        )}
        {open ? (
          <div className="flex items-center gap-2">
            <Button size="sm" onClick={() => onDecide("approve")} disabled={deciding !== null || waitingFor !== null}>
              <Check aria-hidden="true" />
              {deciding === "approve" ? "Approving" : fix.kind === "pr" ? "Approve and open PR" : "Approve and run"}
            </Button>
            <Button size="sm" variant="outline" onClick={() => onDecide("reject")} disabled={deciding !== null}>
              <X aria-hidden="true" />
              {deciding === "reject" ? "Rejecting" : "Reject"}
            </Button>
            <span className="text-[12px] text-muted-foreground">
              {waitingFor
                ? `Runs after ${waitingFor}. Approve that first.`
                : fix.kind === "emergency"
                  ? "Changes production now."
                  : "Nothing changes until someone merges it."}
            </span>
          </div>
        ) : pending ? (
          <p className="text-[12.5px] text-muted-foreground">The run ended before anyone decided.</p>
        ) : (
          <DecidedBy approval={approval} />
        )}
      </div>
    </section>
  );
}
