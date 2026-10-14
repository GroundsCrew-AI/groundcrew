"use client";

import { cn } from "cn";
import { AlertTriangle, CheckCircle2, Clock, ExternalLink } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import type { FollowUp } from "@/lib/incidents";

const TONE = {
  waiting: "border-[color-mix(in_oklab,var(--status-waiting)_45%,var(--border))] text-[var(--status-waiting)]",
  ok: "border-[color-mix(in_oklab,var(--status-ok)_45%,var(--border))] text-[var(--status-ok)]",
  failed: "border-[color-mix(in_oklab,var(--status-failed)_45%,var(--border))] text-[var(--status-failed)]",
  muted: "text-muted-foreground",
} as const;

function Sha({ sha }: { sha: string | null | undefined }) {
  return <span className="font-mono">{(sha ?? "").slice(0, 7)}</span>;
}

function DeployRun({ url }: { url: string | null | undefined }) {
  if (!url) return null;
  return (
    <a href={url} target="_blank" rel="noreferrer" className="underline underline-offset-2">
      deploy run
    </a>
  );
}

function PrLink({ followup }: { followup: FollowUp }) {
  return (
    <a
      href={followup.url ?? undefined}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-0.5 font-medium underline underline-offset-2"
    >
      PR #{followup.number}
      <ExternalLink aria-hidden="true" className="size-3" />
      <span className="sr-only">(opens in a new tab)</span>
    </a>
  );
}

function Note({
  tone,
  icon,
  children,
  action,
}: {
  tone: keyof typeof TONE;
  icon: ReactNode;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className={cn("flex items-start gap-1.5 rounded-md border px-3 py-2 text-[13px]", TONE[tone])}>
      <span className="mt-0.5 shrink-0">{icon}</span>
      <p role="status" className="min-w-0 flex-1">
        {children}
      </p>
      {action}
    </div>
  );
}

/** A change_in_repo follow-up: the service runs on a rollback and no fix PR is on its way. */
function ChangeInRepoNote({
  followup,
  onDismiss,
  dismissing,
}: {
  followup: FollowUp;
  onDismiss?: (id: string) => void;
  dismissing?: boolean;
}) {
  const icon = "size-3.5";
  switch (followup.state) {
    case "reverted":
      return (
        <Note tone="ok" icon={<CheckCircle2 aria-hidden="true" className={icon} />}>
          Commit <Sha sha={followup.triggering_commit} /> is no longer in the deployed code: a later deploy reverted
          it (<DeployRun url={followup.deploy_url} />).
        </Note>
      );
    case "dismissed":
      return (
        <Note tone="muted" icon={<CheckCircle2 aria-hidden="true" className={icon} />}>
          Commit <Sha sha={followup.triggering_commit} /> still in the repo: dismissed
          {followup.dismissed_by ? ` by ${followup.dismissed_by}` : ""}.
        </Note>
      );
    default:
      return (
        <Note
          tone="failed"
          icon={<AlertTriangle aria-hidden="true" className={icon} />}
          action={
            onDismiss && (
              <Button size="sm" variant="outline" disabled={dismissing} onClick={() => onDismiss(followup.id)}>
                {dismissing ? "Dismissing" : "Dismiss"}
              </Button>
            )
          }
        >
          Running on a rollback; the repo still contains commit <Sha sha={followup.triggering_commit} />, so the next
          deploy will bring the failure back.
        </Note>
      );
  }
}

/**
 * The permanent fix after an incident resolved (a rollback can resolve it while the fix PR is
 * open): pending, merged and deploying, deployed, or closed without merging.
 */
export function FollowUpNote({
  followup,
  onDismiss,
  dismissing,
}: {
  followup: FollowUp;
  onDismiss?: (id: string) => void;
  dismissing?: boolean;
}) {
  const icon = "size-3.5";
  if (followup.kind === "change_in_repo") {
    return <ChangeInRepoNote followup={followup} onDismiss={onDismiss} dismissing={dismissing} />;
  }
  switch (followup.state) {
    case "open":
      return (
        <Note tone="waiting" icon={<Clock aria-hidden="true" className={icon} />}>
          Permanent fix pending: <PrLink followup={followup} /> not merged yet. The repository still has the change
          that caused this, so the next deploy would bring it back.
        </Note>
      );
    case "merged":
      return (
        <Note tone="waiting" icon={<Clock aria-hidden="true" className={icon} />}>
          <PrLink followup={followup} /> merged, deploying...
        </Note>
      );
    case "deployed":
      return (
        <Note tone="ok" icon={<CheckCircle2 aria-hidden="true" className={icon} />}>
          Permanent fix deployed (<PrLink followup={followup} />
          {followup.deploy_url && (
            <>
              ,{" "}
              <a href={followup.deploy_url} target="_blank" rel="noreferrer" className="underline underline-offset-2">
                deploy run
              </a>
            </>
          )}
          ).
        </Note>
      );
    case "closed_unmerged":
    default:
      return (
        <Note tone="failed" icon={<AlertTriangle aria-hidden="true" className={icon} />}>
          <PrLink followup={followup} /> was closed without merging. The running service is on a rollback, but the repo
          still contains the change
          {followup.triggering_commit && (
            <>
              {" "}from commit <span className="font-mono">{followup.triggering_commit.slice(0, 7)}</span>
            </>
          )}
          ; the next deploy will bring the failure back.
        </Note>
      );
  }
}

export function FollowUpNotes({
  followups,
  onDismiss,
  dismissing = null,
}: {
  followups: FollowUp[];
  onDismiss?: (id: string) => void;
  /** The follow-up being dismissed now. */
  dismissing?: string | null;
}) {
  if (followups.length === 0) return null;
  return (
    <div className="space-y-2">
      {followups.map((f) => (
        <FollowUpNote key={f.id} followup={f} onDismiss={onDismiss} dismissing={dismissing === f.id} />
      ))}
    </div>
  );
}
