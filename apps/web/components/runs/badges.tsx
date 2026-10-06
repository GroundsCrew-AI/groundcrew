import { cn } from "cn";
import type { CSSProperties } from "react";

import type { RunStatus, StopReason, ToolPolicy } from "@/lib/api/client";

const STATUS: Record<RunStatus, { label: string; color: string }> = {
  queued: { label: "Queued", color: "var(--status-neutral)" },
  running: { label: "Running", color: "var(--status-running)" },
  waiting_approval: { label: "Needs approval", color: "var(--status-waiting)" },
  succeeded: { label: "Succeeded", color: "var(--status-ok)" },
  failed: { label: "Failed", color: "var(--status-failed)" },
  stopped: { label: "Stopped", color: "var(--status-neutral)" },
};

export function statusLabel(status: RunStatus): string {
  return STATUS[status].label;
}

export function StatusPill({ status, className }: { status: RunStatus; className?: string }) {
  const { label, color } = STATUS[status];
  const live = status === "running" || status === "queued";
  return (
    <span
      className={cn(
        "tint inline-flex h-5 items-center gap-1.5 rounded-full px-2 text-[11.5px] font-medium whitespace-nowrap",
        className,
      )}
      style={{ "--tint": color } as CSSProperties}
    >
      <span
        aria-hidden="true"
        className={cn("size-1.5 rounded-full bg-current", status === "running" && "animate-live")}
      />
      {label}
      {live && <span className="sr-only">, in progress</span>}
    </span>
  );
}

const STOP_REASONS: Record<StopReason, string> = {
  completed: "Completed",
  max_steps: "Hit the step limit",
  budget: "Hit a token budget",
  waiting_approval: "Waiting for approval",
  error: "Stopped on an error",
  stopped_by_user: "Stopped by a user",
};

export function stopReasonLabel(reason: StopReason | null | undefined): string | null {
  return reason ? STOP_REASONS[reason] : null;
}

const TIERS: Record<string, { label: string; color: string }> = {
  nano: { label: "Nano", color: "var(--tier-nano)" },
  super: { label: "Super", color: "var(--tier-super)" },
  ultra: { label: "Ultra", color: "var(--tier-ultra)" },
};

export function TierBadge({ tier }: { tier: string | null | undefined }) {
  if (!tier) return null;
  const known = TIERS[tier] ?? { label: tier, color: "var(--status-neutral)" };
  return (
    <span
      className="tint inline-flex h-[18px] items-center rounded px-1.5 text-[11px] font-medium"
      style={{ "--tint": known.color } as CSSProperties}
      title={`Nemotron ${known.label}`}
    >
      {known.label}
    </span>
  );
}

const POLICIES: Record<ToolPolicy, { label: string; color: string; hint: string }> = {
  read: { label: "read", color: "var(--status-neutral)", hint: "Runs without approval" },
  write: { label: "write", color: "var(--status-waiting)", hint: "Needs approval to run" },
  emergency: {
    label: "emergency",
    color: "var(--status-failed)",
    hint: "Emergency action, needs approval",
  },
};

export function PolicyTag({ policy }: { policy: ToolPolicy | null | undefined }) {
  if (!policy) return null;
  const { label, color, hint } = POLICIES[policy];
  return (
    <span
      className="tint inline-flex h-[18px] items-center rounded px-1.5 font-mono text-[10.5px]"
      style={{ "--tint": color } as CSSProperties}
      title={hint}
    >
      {label}
    </span>
  );
}
