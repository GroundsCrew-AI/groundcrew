const numberFormat = new Intl.NumberFormat("en-US");
const compactFormat = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

export function formatTokens(value: number | null | undefined): string {
  if (value === null || value === undefined) return "0";
  return value >= 100_000 ? compactFormat.format(value) : numberFormat.format(value);
}

export function formatCost(value: number | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  if (value === 0) return "$0";
  if (value < 0.01) return `$${value.toFixed(4)}`;
  return `$${value.toFixed(2)}`;
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "";
  if (ms < 1000) return `${Math.max(0, Math.round(ms))} ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)} s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  if (minutes < 60) return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

export function elapsedMs(start: string | null | undefined, end?: string | null, now = Date.now()) {
  if (!start) return null;
  const from = Date.parse(start);
  const to = end ? Date.parse(end) : now;
  return Math.max(0, to - from);
}

const relative = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["day", 86_400],
  ["hour", 3_600],
  ["minute", 60],
];

export function formatRelative(iso: string, now = Date.now()): string {
  const seconds = Math.round((Date.parse(iso) - now) / 1000);
  if (Math.abs(seconds) < 45) return "just now";
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size || unit === "minute") {
      return relative.format(Math.round(seconds / size), unit);
    }
  }
  return "just now";
}

export function formatTimestamp(iso: string): string {
  return new Date(iso).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** "echo_ops" -> "Echo Ops" */
export function agentName(agent: string): string {
  return agent
    .split(/[_-]+/)
    .filter(Boolean)
    .map((part) => part[0]!.toUpperCase() + part.slice(1))
    .join(" ");
}

export function prettyJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}
