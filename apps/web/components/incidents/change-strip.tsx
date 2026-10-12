import { formatTimestamp } from "@/lib/format";
import type { ChangeMarker, IncidentDetail } from "@/lib/incidents";

const COLORS: Record<ChangeMarker["kind"], string> = {
  commit: "var(--tier-super)",
  deploy: "var(--status-running)",
  secret_change: "var(--status-waiting)",
  config: "var(--status-waiting)",
  other: "var(--subtle)",
};

const LABEL: Record<ChangeMarker["kind"], string> = {
  commit: "Commit",
  deploy: "Deploy",
  secret_change: "Secret change",
  config: "Config change",
  other: "Change",
};

/** Where in time a change falls inside the window, 0 to 100. */
export function position(at: string, start: string, end: string): number {
  const s = Date.parse(start);
  const e = Date.parse(end);
  if (!(e > s)) return 0;
  return Math.min(100, Math.max(0, ((Date.parse(at) - s) / (e - s)) * 100));
}

/**
 * Deploys, commits, and secret changes on the incident window, with the first signal marked,
 * then the same changes as a list (the strip is decoration for sighted users).
 */
export function ChangeStrip({
  changes,
  windowStart,
  windowEnd,
  signals,
}: {
  changes: ChangeMarker[];
  windowStart: string;
  windowEnd: string | null;
  signals: IncidentDetail["signals"];
}) {
  const end = windowEnd ?? new Date().toISOString();
  const first = signals.map((s) => s.at).sort()[0];
  return (
    <div className="space-y-2">
      <div aria-hidden="true" className="relative h-6 rounded-md border bg-background">
        {changes.map((c, i) => (
          <span
            key={`${c.ref}-${i}`}
            title={`${LABEL[c.kind]}: ${c.label}`}
            className="absolute top-1 h-4 w-1 rounded-full"
            style={{ left: `${position(c.at, windowStart, end)}%`, background: COLORS[c.kind] }}
          />
        ))}
        {first && (
          <span
            title="First signal"
            className="absolute top-0 h-full w-px bg-[var(--status-failed)]"
            style={{ left: `${position(first, windowStart, end)}%` }}
          />
        )}
      </div>
      <ul className="space-y-0.5 text-[12.5px]">
        {changes.map((c, i) => (
          <li key={`${c.ref}-${i}`} className="flex flex-wrap items-baseline gap-x-2">
            <span className="size-2 shrink-0 rounded-full" style={{ background: COLORS[c.kind] }} aria-hidden="true" />
            <time dateTime={c.at} className="tabular text-muted-foreground">{formatTimestamp(c.at)}</time>
            <span className="text-muted-foreground">{LABEL[c.kind]}</span>
            {c.url ? (
              <a href={c.url} target="_blank" rel="noreferrer" className="underline-offset-4 hover:underline">{c.label}</a>
            ) : (
              <span>{c.label}</span>
            )}
          </li>
        ))}
        {first && (
          <li className="flex flex-wrap items-baseline gap-x-2">
            <span className="h-2 w-px shrink-0 bg-[var(--status-failed)]" aria-hidden="true" />
            <time dateTime={first} className="tabular text-muted-foreground">{formatTimestamp(first)}</time>
            <span className="text-[var(--status-failed)]">First signal</span>
          </li>
        )}
      </ul>
    </div>
  );
}
