"use client";

import { cn } from "cn";
import { useState } from "react";

import { IncidentList, IncidentListHeader } from "@/components/incidents/incident-list";
import { ReportIncidentDialog } from "@/components/incidents/report-dialog";
import { PageBody, PageHeader } from "@/components/shell/page-header";
import { IncidentsIllustration } from "@/components/states/illustrations";
import { EmptyState, ErrorState, RowsSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { type IncidentFilters, type IncidentStatus, useIncidents } from "@/lib/incidents";
import { useWorkspace } from "@/lib/workspace";

const TABS: { label: string; status?: IncidentStatus; pendingFix?: boolean }[] = [
  { label: "All" },
  { label: "Needs approval", status: "waiting_approval" },
  { label: "Resolved", status: "resolved" },
  { label: "Fix pending", pendingFix: true },
  { label: "Not resolved", status: "failed" },
  // Also in "All", each linking to the incident that took it over.
  { label: "Merged", status: "merged" },
];
const PENDING_TAB = TABS.findIndex((t) => t.pendingFix);

export function IncidentsHome({ initialPendingFix = false }: { initialPendingFix?: boolean }) {
  const { workspace, error: workspaceError, refetch } = useWorkspace();
  const [tab, setTab] = useState(initialPendingFix ? PENDING_TAB : 0);
  const { status, pendingFix } = TABS[tab] ?? {};
  const filters: IncidentFilters = {
    ...(status ? { status } : {}),
    ...(pendingFix ? { pending_fix: true } : {}),
  };
  const filtered = Boolean(status || pendingFix);
  const incidents = useIncidents(workspace?.id, filters);
  const items = incidents.data?.pages.flatMap((page) => page.items) ?? [];
  const error = workspaceError ?? incidents.error;

  return (
    <>
      <PageHeader title="Incidents" actions={<ReportIncidentDialog />} />
      <PageBody className="p-0 md:p-0">
        <div role="tablist" aria-label="Filter incidents" className="flex gap-1 border-b px-4 py-2 md:px-6">
          {TABS.map((t, i) => (
            <button
              key={t.label}
              role="tab"
              aria-selected={tab === i}
              onClick={() => setTab(i)}
              className={cn(
                "rounded-md px-2.5 py-1 text-[12.5px] text-muted-foreground hover:text-foreground",
                tab === i && "bg-accent text-foreground",
              )}
            >
              {t.label}
            </button>
          ))}
        </div>
        {error ? (
          <div className="p-4 md:p-6">
            <ErrorState error={error} onRetry={() => (workspaceError ? refetch() : void incidents.refetch())} />
          </div>
        ) : incidents.isLoading || !workspace ? (
          <RowsSkeleton rows={5} />
        ) : items.length === 0 ? (
          <div className="px-4 md:px-6">
            <EmptyState
              illustration={<IncidentsIllustration />}
              title={filtered ? "Nothing here" : "No incidents"}
              action={filtered ? undefined : <ReportIncidentDialog />}
            >
              Medic watches your linked services and opens an incident when health checks fail, tasks crash, errors
              spike, or the database slows down. You can also report one yourself.
            </EmptyState>
          </div>
        ) : (
          <>
            <IncidentListHeader />
            <IncidentList incidents={items} />
            {incidents.hasNextPage && (
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
            )}
          </>
        )}
      </PageBody>
    </>
  );
}
