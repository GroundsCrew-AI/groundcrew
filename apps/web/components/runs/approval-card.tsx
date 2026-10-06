"use client";

import { cn } from "cn";
import { Check, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { UserAvatar } from "@/components/user-avatar";
import type { Approval, Decision } from "@/lib/api/client";
import { formatRelative, formatTimestamp, prettyJson } from "@/lib/format";

import { PolicyTag } from "./badges";

/** The exact action an approval would run, as code: tool_name({...}). */
export function ActionCode({ approval, className }: { approval: Approval; className?: string }) {
  const args = Object.keys(approval.arguments).length ? prettyJson(approval.arguments) : "{}";
  return (
    <pre
      className={cn(
        "overflow-x-auto rounded-md border bg-background px-3 py-2 font-mono text-[12px] leading-relaxed",
        className,
      )}
    >
      <span className="text-foreground">{approval.tool_name}</span>
      <span className="text-muted-foreground">(</span>
      <span className="text-muted-foreground">{args}</span>
      <span className="text-muted-foreground">)</span>
    </pre>
  );
}

export function DecidedBy({ approval }: { approval: Approval }) {
  const approved = approval.status === "approved";
  const who = approval.decided_by_name || approval.decided_by_login || "a teammate";
  return (
    <p className="flex flex-wrap items-center gap-1.5 text-[12.5px] text-muted-foreground">
      {approved ? (
        <Check aria-hidden="true" className="size-3.5 text-[var(--status-ok)]" />
      ) : (
        <X aria-hidden="true" className="size-3.5 text-[var(--status-failed)]" />
      )}
      <span className="text-foreground">{approved ? "Approved" : "Rejected"}</span>
      <span>by</span>
      <UserAvatar src={approval.decided_by_avatar_url} name={who} className="size-4" />
      <span className="text-foreground">{who}</span>
      {approval.decided_at && (
        <time dateTime={approval.decided_at} title={formatTimestamp(approval.decided_at)}>
          {formatRelative(approval.decided_at)}
        </time>
      )}
    </p>
  );
}

/**
 * Inline approval in the run timeline. Shows exactly what will run; Approve and Reject act
 * right away (the card itself is the confirmation: it repeats the exact action).
 */
export function ApprovalCard({
  approval,
  onDecide,
  deciding,
  runEnded = false,
}: {
  approval: Approval;
  onDecide: (decision: Decision) => void;
  deciding: Decision | null;
  runEnded?: boolean;
}) {
  const pending = approval.status === "pending";
  const open = pending && !runEnded;
  return (
    <section
      aria-label={`Approval for ${approval.tool_name}`}
      className={cn(
        "rounded-lg border bg-card",
        open && "border-[color-mix(in_oklab,var(--status-waiting)_45%,var(--border))]",
      )}
    >
      <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
        <span className="text-[13px] font-medium">
          {open ? "Approval needed" : pending ? "Not decided" : "Approval"}
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
        <p className="text-[12.5px] text-muted-foreground">
          {open
            ? "The agent wants to run this action. Nothing runs until you approve it."
            : "The agent asked to run this action."}
        </p>
        <ActionCode approval={approval} />
        {open ? (
          <div className="flex items-center gap-2">
            <Button size="sm" onClick={() => onDecide("approve")} disabled={deciding !== null}>
              <Check aria-hidden="true" />
              {deciding === "approve" ? "Approving" : "Approve"}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => onDecide("reject")}
              disabled={deciding !== null}
            >
              <X aria-hidden="true" />
              {deciding === "reject" ? "Rejecting" : "Reject"}
            </Button>
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
