"use client";

import { cn } from "cn";
import { Search, X } from "lucide-react";
import { usePathname, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { IncidentList, IncidentListHeader } from "@/components/incidents/incident-list";
import { ReportIncidentDialog } from "@/components/incidents/report-dialog";
import { PageBody, PageHeader } from "@/components/shell/page-header";
import { IncidentsIllustration } from "@/components/states/illustrations";
import { EmptyState, ErrorState, RowsSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  apiIncidentFilters,
  emptyTitle,
  hasFilters,
  INCIDENT_TABS,
  INCIDENT_WINDOWS,
  type IncidentListFilters,
  incidentFiltersSearch,
  matchesSearch,
  NO_FILTERS,
  parseIncidentFilters,
} from "@/lib/incident-filters";
import { type IncidentWindow, SEVERITY, type Severity, useIncidents } from "@/lib/incidents";
import { useWorkspace } from "@/lib/workspace";

const ANY = "any";

/**
 * The incident list. Filters are read from the URL and written back with the History API, which
 * the App Router keeps in sync with useSearchParams: picking a filter adds a history entry (so
 * back/forward step through filters and a link to the page carries them), typing a search
 * replaces the current one. Nothing here reloads the page.
 */
export function IncidentsHome() {
  const { workspace, error: workspaceError, refetch } = useWorkspace();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const filters = useMemo(() => parseIncidentFilters(searchParams), [searchParams]);

  // The search box keeps its own text while typing; the URL follows it. When the URL changes
  // from elsewhere (back/forward, a link), the box follows the URL.
  const [text, setText] = useState(filters.q);
  const written = useRef(filters.q);
  useEffect(() => {
    if (filters.q !== written.current) {
      written.current = filters.q;
      setText(filters.q);
    }
  }, [filters.q]);

  function navigate(next: IncidentListFilters, mode: "push" | "replace" = "push") {
    const url = `${pathname}${incidentFiltersSearch(next)}`;
    if (mode === "push") window.history.pushState(null, "", url);
    else window.history.replaceState(null, "", url);
  }
  const update = (patch: Partial<IncidentListFilters>) => navigate({ ...filters, q: text, ...patch });
  function search(value: string) {
    setText(value);
    written.current = value;
    navigate({ ...filters, q: value }, "replace");
  }
  function clear() {
    setText("");
    written.current = "";
    navigate(NO_FILTERS);
  }

  const incidents = useIncidents(workspace?.id, apiIncidentFilters(filters));
  const loaded = incidents.data?.pages.flatMap((page) => page.items) ?? [];
  // The API has no text search, so the search narrows the incidents loaded so far.
  const items = text.trim() ? loaded.filter((i) => matchesSearch(i, text)) : loaded;
  const error = workspaceError ?? incidents.error;
  const current = { ...filters, q: text };
  const filtered = hasFilters(current);

  const loadOlder = incidents.hasNextPage && (
    <div className="border-t px-4 py-3">
      <Button
        variant="ghost"
        size="sm"
        onClick={() => void incidents.fetchNextPage()}
        disabled={incidents.isFetchingNextPage}
      >
        {incidents.isFetchingNextPage ? "Loading" : "Load older incidents"}
      </Button>
    </div>
  );

  return (
    <>
      <PageHeader title="Incidents" actions={<ReportIncidentDialog />} />
      <PageBody className="p-0 md:p-0">
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b px-4 py-2 md:px-6">
          {/* Toggle buttons, not tabs: each one filters the one list below rather than showing its
              own panel. The pressed one is the current status filter. */}
          <div role="group" aria-label="Filter incidents by status" className="-ml-1 flex flex-wrap gap-1">
            {INCIDENT_TABS.map((t) => (
              <button
                key={t.id}
                type="button"
                aria-pressed={filters.tab === t.id}
                onClick={() => update({ tab: t.id })}
                className={cn(
                  "rounded-md px-2.5 py-1 text-[12.5px] text-muted-foreground hover:text-foreground",
                  filters.tab === t.id && "bg-accent text-foreground",
                )}
              >
                {t.label}
              </button>
            ))}
          </div>
          <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto">
            <div className="relative min-w-0 flex-1 sm:w-56 sm:flex-none">
              <Search
                aria-hidden="true"
                className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground"
              />
              <Input
                type="search"
                value={text}
                onChange={(e) => search(e.target.value)}
                aria-label="Search incidents by title or resource"
                placeholder="Search title or resource"
                className="h-7 pl-8 text-[12.5px] md:text-[12.5px]"
              />
            </div>
            <Select
              value={filters.severity ?? ANY}
              onValueChange={(v) => update({ severity: v === ANY ? null : (v as Severity) })}
            >
              <SelectTrigger size="sm" aria-label="Severity" className="text-[12.5px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent align="end">
                <SelectItem value={ANY}>Any severity</SelectItem>
                {(Object.keys(SEVERITY) as Severity[]).map((s) => (
                  <SelectItem key={s} value={s}>
                    {SEVERITY[s].label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={filters.window ?? "all"}
              onValueChange={(v) => update({ window: v === "all" ? null : (v as IncidentWindow) })}
            >
              <SelectTrigger size="sm" aria-label="Opened" className="text-[12.5px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent align="end">
                {INCIDENT_WINDOWS.map((w) => (
                  <SelectItem key={w.id} value={w.id}>
                    {w.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {filtered && (
              <Button variant="ghost" size="sm" className="h-7 text-muted-foreground" onClick={clear}>
                <X aria-hidden="true" />
                Clear
              </Button>
            )}
          </div>
        </div>
        {error ? (
          <div className="p-4 md:p-6">
            <ErrorState error={error} onRetry={() => (workspaceError ? refetch() : void incidents.refetch())} />
          </div>
        ) : incidents.isLoading || !workspace ? (
          <RowsSkeleton rows={5} />
        ) : items.length === 0 ? (
          <>
            <div className="px-4 md:px-6">
              {text.trim() ? (
                <EmptyState
                  title={`No incidents match "${text.trim()}"`}
                  action={
                    <Button size="sm" variant="outline" onClick={() => search("")}>
                      Clear search
                    </Button>
                  }
                >
                  The search looks at the titles and affected resources of the{" "}
                  {loaded.length === 1 ? "incident" : `${loaded.length} incidents`} loaded so far
                  {filtered && loaded.length > 0 ? " with these filters" : ""}.
                  {incidents.hasNextPage ? " Load older incidents to search further back." : ""}
                </EmptyState>
              ) : filtered ? (
                <EmptyState
                  illustration={<IncidentsIllustration />}
                  title={emptyTitle(current)}
                  action={
                    <Button size="sm" variant="outline" onClick={clear}>
                      Clear filters
                    </Button>
                  }
                >
                  Try another status, severity, or time range, or clear the filters to see every incident.
                </EmptyState>
              ) : (
                <EmptyState
                  illustration={<IncidentsIllustration />}
                  title="No incidents"
                  action={<ReportIncidentDialog />}
                >
                  Medic watches your linked services and opens an incident when health checks fail, tasks
                  crash, errors spike, or the database slows down. You can also report one yourself.
                </EmptyState>
              )}
            </div>
            {text.trim() && loadOlder}
          </>
        ) : (
          <>
            <IncidentListHeader />
            <IncidentList incidents={items} />
            {loadOlder}
          </>
        )}
      </PageBody>
    </>
  );
}
