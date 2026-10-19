import {
  type IncidentFilters,
  type IncidentStatus,
  type IncidentSummary,
  type IncidentWindow,
  isIncidentWindow,
  SEVERITY,
  type Severity,
} from "@/lib/incidents";

/**
 * The incidents list's filters live in the URL (?status=resolved&severity=high&window=7d&q=api),
 * so a filtered list can be shared, survives a reload, and back/forward walk through filters.
 * Status, severity, and the time window go to the API; the text search does not exist there, so
 * it filters the incidents loaded so far in the browser.
 */

export type IncidentTab = "all" | "waiting_approval" | "resolved" | "pending_fix" | "failed" | "merged";

export const INCIDENT_TABS: { id: IncidentTab; label: string }[] = [
  { id: "all", label: "All" },
  { id: "waiting_approval", label: "Needs approval" },
  { id: "resolved", label: "Resolved" },
  { id: "pending_fix", label: "Fix pending" },
  { id: "failed", label: "Not resolved" },
  // Also in "All", each linking to the incident that took it over.
  { id: "merged", label: "Merged" },
];

const TAB_STATUS: Partial<Record<IncidentTab, IncidentStatus>> = {
  waiting_approval: "waiting_approval",
  resolved: "resolved",
  failed: "failed",
  merged: "merged",
};

export const INCIDENT_WINDOWS: { id: IncidentWindow | "all"; label: string }[] = [
  { id: "all", label: "Any time" },
  { id: "24h", label: "Last 24 hours" },
  { id: "7d", label: "Last 7 days" },
  { id: "30d", label: "Last 30 days" },
];

export type IncidentListFilters = {
  tab: IncidentTab;
  severity: Severity | null;
  window: IncidentWindow | null;
  /** Text search over titles and affected resources, as typed. */
  q: string;
};

export const NO_FILTERS: IncidentListFilters = { tab: "all", severity: null, window: null, q: "" };

type Params = { get(name: string): string | null };

/** Read filters from the URL. Unknown values are ignored rather than sent to the API. */
export function parseIncidentFilters(params: Params): IncidentListFilters {
  const status = params.get("status");
  const tab: IncidentTab =
    params.get("pending_fix") === "true"
      ? "pending_fix"
      : (Object.keys(TAB_STATUS) as IncidentTab[]).find((t) => TAB_STATUS[t] === status) ?? "all";
  const severity = params.get("severity");
  const window = params.get("window");
  return {
    tab,
    severity: severity && Object.hasOwn(SEVERITY, severity) ? (severity as Severity) : null,
    window: isIncidentWindow(window) ? window : null,
    q: params.get("q") ?? "",
  };
}

/** The query string for filters, "" when none are set. Defaults are left out. */
export function incidentFiltersSearch(filters: IncidentListFilters): string {
  const params = new URLSearchParams();
  const status = TAB_STATUS[filters.tab];
  if (status) params.set("status", status);
  if (filters.tab === "pending_fix") params.set("pending_fix", "true");
  if (filters.severity) params.set("severity", filters.severity);
  if (filters.window) params.set("window", filters.window);
  if (filters.q.trim()) params.set("q", filters.q);
  const search = params.toString();
  return search ? `?${search}` : "";
}

/** What the API filters on. The text search is applied in the browser (see matchesSearch). */
export function apiIncidentFilters(filters: IncidentListFilters): IncidentFilters {
  const status = TAB_STATUS[filters.tab];
  return {
    ...(status ? { status } : {}),
    ...(filters.tab === "pending_fix" ? { pending_fix: true } : {}),
    ...(filters.severity ? { severity: filters.severity } : {}),
    ...(filters.window ? { window: filters.window } : {}),
  };
}

export function hasFilters(filters: IncidentListFilters): boolean {
  return filters.tab !== "all" || Boolean(filters.severity || filters.window || filters.q.trim());
}

/**
 * Client-side text search: the API has no text filter, so this narrows the incidents fetched so
 * far (title and affected resource names, case-insensitive, every word must match).
 */
export function matchesSearch(incident: IncidentSummary, q: string): boolean {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return true;
  const haystack = [incident.title, ...incident.affected_resources.map((r) => r.name)].join(" ").toLowerCase();
  return words.every((word) => haystack.includes(word));
}

const TAB_PHRASE: Record<IncidentTab, string> = {
  all: "",
  waiting_approval: "waiting for approval",
  resolved: "resolved",
  pending_fix: "with a permanent fix pending",
  failed: "not resolved",
  merged: "merged into another incident",
};

const WINDOW_PHRASE: Record<IncidentWindow, string> = {
  "24h": "in the last 24 hours",
  "7d": "in the last 7 days",
  "30d": "in the last 30 days",
};

/** "No high severity incidents waiting for approval in the last 7 days", for the empty state. */
export function emptyTitle(filters: IncidentListFilters): string {
  const severity = filters.severity ? `${SEVERITY[filters.severity].label.toLowerCase()} severity ` : "";
  const parts = [`No ${severity}incidents`, TAB_PHRASE[filters.tab], filters.window ? WINDOW_PHRASE[filters.window] : ""];
  return parts.filter(Boolean).join(" ");
}
