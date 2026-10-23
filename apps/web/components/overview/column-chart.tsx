"use client";

import { cn } from "cn";
import { type CSSProperties, type ReactNode, useId, useState } from "react";

/**
 * Daily columns for the overview, drawn in plain HTML: one column per day, stacked when there
 * are several series. Marks carry the series color; every label stays in text tokens. Hover or
 * focus a day for its values; the same numbers are in the table under "Show table".
 */

export type Series = { key: string; label: string; color: string; values: number[] };

/** A clean axis top: 1, 2, 5 times a power of ten, at least `max`. */
export function niceMax(max: number): number {
  if (max <= 0) return 1;
  const power = 10 ** Math.floor(Math.log10(max));
  for (const step of [1, 2, 5, 10]) {
    if (step * power >= max) return step * power;
  }
  return 10 * power;
}

const dayFormat = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" });

/** "2026-10-23" -> "Oct 23" (the API's days are UTC). */
export function dayLabel(day: string): string {
  return dayFormat.format(new Date(`${day}T00:00:00Z`));
}

export function ColumnChart({
  label,
  days,
  series,
  format = (v) => String(v),
  reference,
  height = 140,
}: {
  /** What the chart shows, for screen readers and the table caption. */
  label: string;
  /** ISO days, oldest first. */
  days: string[];
  series: Series[];
  format?: (value: number) => string;
  /** A horizontal line on the same axis (a daily cap), with its label. */
  reference?: { value: number; label: string };
  height?: number;
}) {
  const id = useId();
  const [active, setActive] = useState<number | null>(null);
  const totals = days.map((_, i) => series.reduce((sum, s) => sum + (s.values[i] ?? 0), 0));
  const top = niceMax(Math.max(...totals, reference && reference.value > 0 ? reference.value : 0));
  const ticks = [top, top / 2, 0];
  const percent = (value: number) => `${(value / top) * 100}%`;

  return (
    <figure className="space-y-3">
      {series.length > 1 && (
        <figcaption className="flex flex-wrap gap-x-4 gap-y-1 text-[12px] text-muted-foreground">
          {series.map((s) => (
            <span key={s.key} className="inline-flex items-center gap-1.5">
              <span aria-hidden="true" className="size-2 rounded-[2px]" style={{ background: s.color }} />
              {s.label}
            </span>
          ))}
        </figcaption>
      )}
      <div className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-2">
        <div aria-hidden="true" className="tabular relative w-10 text-right text-[11px] text-subtle" style={{ height }}>
          {ticks.map((t) => (
            <span key={t} className="absolute right-0 -translate-y-1/2" style={{ top: `${100 - (t / top) * 100}%` }}>
              {format(t)}
            </span>
          ))}
        </div>
        <div className="relative" style={{ height }} onMouseLeave={() => setActive(null)}>
          {ticks.map((t) => (
            <div
              key={t}
              aria-hidden="true"
              className="absolute inset-x-0 border-t border-border"
              style={{ top: `${100 - (t / top) * 100}%` }}
            />
          ))}
          {reference && reference.value > 0 && reference.value <= top && (
            <div
              aria-hidden="true"
              className="absolute inset-x-0 border-t border-[var(--status-waiting)]"
              style={{ bottom: percent(reference.value) }}
            >
              <span className="absolute right-0 -translate-y-full bg-card pl-1 text-[11px] text-muted-foreground">
                {reference.label}
              </span>
            </div>
          )}
          <div role="list" aria-label={label} className="absolute inset-0 flex items-end gap-[2px]">
            {days.map((day, i) => {
              const parts = series.filter((s) => (s.values[i] ?? 0) > 0);
              const summary = `${dayLabel(day)}: ${
                series.length > 1
                  ? `${format(totals[i] ?? 0)} total${parts.map((s) => `, ${s.label} ${format(s.values[i] ?? 0)}`).join("")}`
                  : format(totals[i] ?? 0)
              }`;
              return (
                <div
                  key={day}
                  role="listitem"
                  tabIndex={0}
                  aria-label={summary}
                  aria-describedby={active === i ? `${id}-tip` : undefined}
                  onMouseEnter={() => setActive(i)}
                  onFocus={() => setActive(i)}
                  onBlur={() => setActive(null)}
                  className={cn(
                    "group relative flex h-full min-w-0 flex-1 flex-col justify-end rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
                    active === i && "bg-accent/60",
                  )}
                >
                  <div
                    className="mx-auto flex w-full max-w-6 flex-col-reverse gap-[2px]"
                    style={{ height: percent(totals[i] ?? 0) }}
                  >
                    {parts.map((s, j) => (
                      <div
                        key={s.key}
                        className={cn("w-full", j === parts.length - 1 && "rounded-t-[4px]")}
                        // Shares of the column, scaled up: grow factors summing under 1 leave the column part empty.
                        style={{
                          flexGrow: ((s.values[i] ?? 0) / (totals[i] || 1)) * 100,
                          flexBasis: 0,
                          minHeight: 2,
                          background: s.color,
                        }}
                      />
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
          {active !== null && days[active] && (
            <Tooltip id={`${id}-tip`} index={active} count={days.length}>
              <p className="font-medium text-foreground">{dayLabel(days[active])}</p>
              {series.length > 1 && (
                <p className="tabular flex justify-between gap-4">
                  <span>Total</span>
                  <span className="text-foreground">{format(totals[active] ?? 0)}</span>
                </p>
              )}
              {series.map((s) => (
                <p key={s.key} className="tabular flex items-center justify-between gap-4">
                  <span className="inline-flex items-center gap-1.5">
                    <span aria-hidden="true" className="size-2 rounded-[2px]" style={{ background: s.color }} />
                    {s.label}
                  </span>
                  <span className="text-foreground">{format(s.values[active] ?? 0)}</span>
                </p>
              ))}
            </Tooltip>
          )}
        </div>
        <div />
        <div aria-hidden="true" className="mt-1.5 flex justify-between text-[11px] text-subtle">
          <span>{days[0] && dayLabel(days[0])}</span>
          <span>{days.length > 2 && dayLabel(days[Math.floor(days.length / 2)]!)}</span>
          <span>{days.length > 1 && dayLabel(days[days.length - 1]!)}</span>
        </div>
      </div>
      <details className="text-[12px] text-muted-foreground">
        <summary className="cursor-pointer select-none hover:text-foreground">Show table</summary>
        <div className="mt-2 max-h-56 overflow-auto rounded-md border">
          <table className="tabular w-full text-left">
            <caption className="sr-only">{label}</caption>
            <thead className="sticky top-0 bg-card">
              <tr className="border-b">
                <th scope="col" className="px-2 py-1 font-medium">Day</th>
                {series.map((s) => (
                  <th key={s.key} scope="col" className="px-2 py-1 text-right font-medium">{s.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {days.map((day, i) => (
                <tr key={day} className="border-b last:border-0">
                  <th scope="row" className="px-2 py-1 font-normal">{dayLabel(day)}</th>
                  {series.map((s) => (
                    <td key={s.key} className="px-2 py-1 text-right text-foreground">{format(s.values[i] ?? 0)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}

function Tooltip({ id, index, count, children }: { id: string; index: number; count: number; children: ReactNode }) {
  // Kept inside the plot: anchored left of the day in the right half, right of it otherwise.
  const center = ((index + 0.5) / count) * 100;
  const style: CSSProperties = center > 50 ? { right: `${100 - center + 2}%` } : { left: `${center + 2}%` };
  return (
    <div
      id={id}
      role="tooltip"
      className="pointer-events-none absolute top-0 z-10 min-w-36 space-y-0.5 rounded-md border bg-popover px-2.5 py-2 text-[12px] text-muted-foreground shadow-md"
      style={style}
    >
      {children}
    </div>
  );
}
