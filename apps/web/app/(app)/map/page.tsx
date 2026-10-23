"use client";

import Link from "next/link";
import { Suspense, useMemo, useState } from "react";

import { RunDiscoveryButton } from "@/components/connections/run-discovery-button";
import { MapCanvas } from "@/components/map/map-canvas";
import { MapFilters } from "@/components/map/map-filters";
import { ResourcePanel } from "@/components/map/resource-panel";
import { SystemSummary } from "@/components/map/system-summary";
import { PageBody, PageHeader } from "@/components/shell/page-header";
import { MapIllustration } from "@/components/states/illustrations";
import { EmptyState, ErrorState } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useGraph } from "@/lib/discovery";
import { formatRelative } from "@/lib/format";
import { DEFAULT_FILTERS, type MapFilters as Filters, visibleGraph } from "@/lib/graph";
import { useSearchParam } from "@/lib/use-search-param";
import { useWorkspace } from "@/lib/workspace";

export default function MapPage() {
  // The open resource lives in the URL (useSearchParams).
  return (
    <Suspense>
      <MapView />
    </Suspense>
  );
}

function MapView() {
  const { workspace, error: workspaceError, refetch } = useWorkspace();
  const [includeStale, setIncludeStale] = useState(false);
  const [filters, setFilters] = useState<Filters>(DEFAULT_FILTERS);
  // /map?resource=<id> opens that resource's panel ("View on Map" on a deploy or an incident links
  // there). Each resource opened adds a history entry, so Back closes the panel or steps back.
  const [selected, setSelected] = useSearchParam("resource");
  const graph = useGraph(workspace?.id, includeStale);
  const data = graph.data;
  const providers = useMemo(() => [...new Set((data?.nodes ?? []).map((n) => n.provider))].sort(), [data]);
  const shown = useMemo(
    () => (data ? visibleGraph(data.nodes, data.edges, filters) : { nodes: [], edges: [] }),
    [data, filters],
  );
  const error = workspaceError ?? graph.error;
  const running = data && ["queued", "running"].includes(data.last_run_status ?? "");

  return (
    <>
      <PageHeader
        title="Map"
        actions={
          <>
            {data && data.nodes.length > 0 && (
              <MapFilters
                providers={providers}
                filters={filters}
                includeStale={includeStale}
                onChange={setFilters}
                onIncludeStale={setIncludeStale}
              />
            )}
            <RunDiscoveryButton />
          </>
        }
      />
      {error ? (
        <PageBody>
          <ErrorState error={error} onRetry={() => (workspaceError ? refetch() : void graph.refetch())} />
        </PageBody>
      ) : !data ? (
        <PageBody className="space-y-4">
          <Skeleton className="h-28 w-full" />
          <Skeleton className="h-[60vh] w-full" />
        </PageBody>
      ) : data.nodes.length === 0 ? (
        <PageBody>
          <EmptyState
            illustration={<MapIllustration />}
            title={running ? "Discovery is running" : "Nothing on the map yet"}
            action={
              running && data.last_run_id ? (
                <Button size="sm" asChild>
                  <Link href={`/runs/${data.last_run_id}`}>Watch it live</Link>
                </Button>
              ) : (
                <div className="flex gap-2">
                  <Button size="sm" variant="outline" asChild>
                    <Link href="/connections">Add a connection</Link>
                  </Button>
                  <RunDiscoveryButton variant="default" />
                </div>
              )
            }
          >
            Connect a cloud account, a cluster, or GitHub, then run discovery. Groundcrew reads each
            connection and works out how the pieces talk to each other, with evidence for every line it
            draws.
          </EmptyState>
        </PageBody>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col">
          <div className="space-y-3 px-4 pt-4 md:px-6">
            <SystemSummary summary={data.summary} />
            <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-muted-foreground">
              <span className="tabular">
                {shown.nodes.length} of {data.nodes.length} resources, {shown.edges.length} relationships
              </span>
              {data.last_run_at && data.last_run_id && (
                <Link href={`/runs/${data.last_run_id}`} className="underline-offset-2 hover:underline">
                  {running ? "Discovery running now" : `Last discovery ${formatRelative(data.last_run_at)}`}
                </Link>
              )}
              {data.stale_count > 0 && !includeStale && <span>{data.stale_count} not seen in the last discovery (hidden)</span>}
            </p>
          </div>
          <div className="relative mt-3 min-h-[640px] flex-1 border-t">
            <MapCanvas nodes={shown.nodes} edges={shown.edges} selectedId={selected} onSelect={setSelected} />
            {selected && (
              <ResourcePanel resourceId={selected} onClose={() => setSelected(null)} onSelect={setSelected} />
            )}
          </div>
        </div>
      )}
    </>
  );
}

