import Link from "next/link";
import type { CSSProperties, ReactNode } from "react";

import { Progress } from "@/components/ui/progress";
import type { Summary } from "@/lib/api/client";
import { formatCost } from "@/lib/format";

/**
 * Model spend on the overview: this workspace's spend today against its own daily cap (set by
 * an owner in Settings, else the platform default), and its total. When the cap is reached,
 * agents stop calling models until the next UTC day.
 */

/** Percent of a cap used, 0 to 100. A zero cap is already full. */
export function usedPercent(used: number, cap: number): number {
  if (cap <= 0) return 100;
  return Math.min(100, Math.max(0, Math.round((used / cap) * 100)));
}

/** "$0", "$0.0042", "$1.20" */
export function formatUsd(value: number | null | undefined): string {
  return formatCost(value ?? 0) ?? "$0";
}

export type Spend = {
  today: number;
  total: number;
  /** Null: no daily cap. */
  cap: number | null;
  /** Null without a cap. */
  percent: number | null;
  reached: boolean;
};

export function spendOf(summary: Pick<Summary, "usd_today" | "usd_total" | "daily_usd_cap">): Spend {
  const today = summary.usd_today ?? 0;
  const total = summary.usd_total ?? 0;
  const cap = summary.daily_usd_cap ?? null;
  if (cap === null) return { today, total, cap, percent: null, reached: false };
  return { today, total, cap, percent: usedPercent(today, cap), reached: today >= cap };
}

/** Bar color: calm under 70%, a warning from 70%, failed from 90%. */
export function spendTone(percent: number): string {
  if (percent >= 90) return "var(--status-failed)";
  if (percent >= 70) return "var(--status-waiting)";
  return "var(--primary)";
}

function Note({ children }: { children: ReactNode }) {
  return <p className="tabular text-[12px] text-muted-foreground">{children}</p>;
}

function SettingsLink({ children }: { children: ReactNode }) {
  return (
    <Link href="/settings" className="text-foreground underline-offset-2 hover:underline">
      {children}
    </Link>
  );
}

export function SpendMeter({ summary }: { summary: Summary }) {
  const spend = spendOf(summary);
  const total = <Note>{formatUsd(spend.total)} in total</Note>;
  if (spend.cap === null || spend.percent === null) {
    return (
      <div className="space-y-0.5">
        <Note>
          No daily cap. <SettingsLink>Set one in Settings</SettingsLink>
        </Note>
        {total}
      </div>
    );
  }
  return (
    <div className="space-y-1.5">
      <Progress
        value={spend.percent}
        aria-label={`${spend.percent}% of today's spend cap used`}
        className="h-1"
        style={{ "--progress-color": spendTone(spend.percent) } as CSSProperties}
      />
      <Note>
        {formatUsd(spend.today)} of {formatUsd(spend.cap)} daily cap
      </Note>
      {spend.reached && (
        <p role="status" className="text-[12px] text-[var(--status-failed)]">
          Cap reached: agents stop calling models until 00:00 UTC. Owners can raise it in{" "}
          <SettingsLink>Settings</SettingsLink>.
        </p>
      )}
      {total}
    </div>
  );
}
