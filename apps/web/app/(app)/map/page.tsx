"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

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
import { useWorkspace } from "@/lib/workspace";

export default function MapPage() {
  const { workspace, error: workspaceError, refetch } = useWorkspace();
  const [includeStale, setIncludeStale] = useState(false);
  const [filters, setFilters] = useState<Filters>(DEFAULT_FILTERS);
  const [selected, setSelected] = useState<string | null>(null);
  // /map?resource=<id> (from "View on Map" on a deploy) opens that resource's panel.
  useEffect(() => {
    const resource = new URLSearchParams(window.location.search).get("resource");
    if (resource) setSelected(resource);
  }, []);
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
              <span className="ml-auto hidden items-center gap-3 sm:flex">
                <Legend dashed={false} label="High or medium confidence" />
                <Legend dashed label="Low confidence" />
              </span>
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

function Legend({ dashed, label }: { dashed: boolean; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <svg aria-hidden="true" width="22" height="6">
        <line x1="0" y1="3" x2="22" y2="3" stroke="var(--subtle)" strokeWidth="1.5" strokeDasharray={dashed ? "4 4" : undefined} />
      </svg>
      {label}
    </span>
  );
}
