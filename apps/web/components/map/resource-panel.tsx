"use client";

import { ArrowLeft, ArrowRight, ExternalLink, FileCode, X } from "lucide-react";

import { ErrorState } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { type ResourceDetail, useResource } from "@/lib/discovery";
import { formatRelative } from "@/lib/format";
import { edgeLabel, PROVIDER_LABELS, typeLabel } from "@/lib/graph";
import { configRows } from "@/lib/resource-summary";
import { useMediaQuery } from "@/lib/use-media-query";

import { ConfidenceTag, CoverageBadge, ResourceIcon } from "./resource-meta";

export function ResourcePanel({
  resourceId,
  onClose,
  onSelect,
}: {
  resourceId: string;
  onClose: () => void;
  onSelect: (id: string) => void;
}) {
  const desktop = useMediaQuery("(min-width: 768px)");
  const body = <PanelBody resourceId={resourceId} onSelect={onSelect} />;
  // Below md a side panel would cover the whole map: show the details in a bottom sheet instead.
  if (!desktop) {
    return (
      <Sheet open onOpenChange={(open) => !open && onClose()}>
        <SheetContent side="bottom" aria-describedby={undefined} className="max-h-[85dvh] gap-0 pt-10">
          <SheetTitle className="sr-only">Resource details</SheetTitle>
          {body}
        </SheetContent>
      </Sheet>
    );
  }
  return (
    <aside
      aria-label="Resource details"
      className="absolute inset-y-0 right-0 z-10 flex w-full max-w-[420px] flex-col border-l bg-background shadow-[-8px_0_24px_rgb(0_0_0/0.08)]"
    >
      <div className="flex items-center justify-end border-b px-3 py-2">
        <Button variant="ghost" size="icon-sm" aria-label="Close details" onClick={onClose}>
          <X />
        </Button>
      </div>
      {body}
    </aside>
  );
}

function PanelBody({ resourceId, onSelect }: { resourceId: string; onSelect: (id: string) => void }) {
  const query = useResource(resourceId);
  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-6">
      {query.error ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} className="mt-4" />
      ) : !query.data ? (
        <div aria-busy="true" className="space-y-3 pt-4">
          <Skeleton className="h-5 w-48" />
          <Skeleton className="h-4 w-32" />
          <Skeleton className="h-24 w-full" />
        </div>
      ) : (
        <ResourceDetails detail={query.data} onSelect={onSelect} />
      )}
    </div>
  );
}

export function ResourceDetails({ detail, onSelect }: { detail: ResourceDetail; onSelect: (id: string) => void }) {
  const rows = configRows(detail.type, detail.config);
  const where = [PROVIDER_LABELS[detail.provider] ?? detail.provider, detail.account, detail.region]
    .filter(Boolean)
    .join(", ");
  return (
    <div className="space-y-6 pt-4">
      <header className="flex gap-3">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-md border bg-card text-muted-foreground">
          <ResourceIcon type={detail.type} className="size-4" />
        </span>
        <div className="min-w-0 space-y-1">
          <h2 className="truncate text-[15px] font-semibold tracking-tight" title={detail.name}>
            {detail.name}
          </h2>
          <p className="text-[12px] text-muted-foreground">
            {typeLabel(detail.type)}, {where}
          </p>
          <div className="flex flex-wrap items-center gap-2 pt-0.5">
            <CoverageBadge coverage={detail.coverage} />
            {detail.stale && <span className="text-[12px] text-subtle">Not seen in the last discovery</span>}
          </div>
        </div>
      </header>

      {rows.length > 0 && (
        <section aria-labelledby="config-title">
          <h3 id="config-title" className="mb-2 text-[12px] font-medium text-muted-foreground">
            Configuration
          </h3>
          <dl className="divide-y rounded-lg border">
            {rows.map((row) => (
              <div key={row.label} className="grid grid-cols-[120px_1fr] gap-3 px-3 py-2 text-[12.5px]">
                <dt className="text-muted-foreground">{row.label}</dt>
                <dd className="min-w-0 break-words font-mono text-[12px]">{row.value}</dd>
              </div>
            ))}
          </dl>
        </section>
      )}

      {detail.iac_path && (
        <section aria-labelledby="iac-title">
          <h3 id="iac-title" className="mb-2 text-[12px] font-medium text-muted-foreground">
            Defined in
          </h3>
          {detail.iac_url ? (
            <a
              href={detail.iac_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1.5 font-mono text-[12px] underline-offset-2 hover:underline"
            >
              <FileCode aria-hidden="true" className="size-3.5" />
              {detail.repo}/{detail.iac_path}
              <ExternalLink aria-hidden="true" className="size-3" />
            </a>
          ) : (
            <p className="font-mono text-[12px]">{detail.iac_path}</p>
          )}
        </section>
      )}

      <section aria-labelledby="edges-title">
        <h3 id="edges-title" className="mb-2 text-[12px] font-medium text-muted-foreground">
          Relationships
        </h3>
        {detail.edges.length === 0 ? (
          <p className="text-[12.5px] text-muted-foreground">No relationships found yet.</p>
        ) : (
          <ul className="space-y-2.5">
            {detail.edges.map((edge) => (
              <li key={edge.id} className="rounded-lg border px-3 py-2.5">
                <div className="flex flex-wrap items-center gap-1.5 text-[12.5px]">
                  {edge.direction === "out" ? (
                    <ArrowRight aria-label="Outgoing" className="size-3.5 text-subtle" />
                  ) : (
                    <ArrowLeft aria-label="Incoming" className="size-3.5 text-subtle" />
                  )}
                  <span className="text-muted-foreground">
                    {edge.direction === "out" ? edgeLabel(edge.kind) : `${edgeLabel(edge.kind)} (from)`}
                  </span>
                  <button
                    type="button"
                    onClick={() => onSelect(edge.other.id)}
                    className="truncate font-medium underline-offset-2 hover:underline"
                  >
                    {edge.other.name}
                  </button>
                  <span className="ml-auto">
                    <ConfidenceTag confidence={edge.confidence} />
                  </span>
                </div>
                <p className="mt-1.5 text-[12px] leading-relaxed text-muted-foreground">{edge.evidence}</p>
              </li>
            ))}
          </ul>
        )}
      </section>

      {detail.recent_deploys.length > 0 && (
        <section aria-labelledby="deploys-title">
          <h3 id="deploys-title" className="mb-2 text-[12px] font-medium text-muted-foreground">
            Recent deploys
          </h3>
          <ul className="divide-y rounded-lg border">
            {detail.recent_deploys.map((deploy, i) => (
              <li key={`${deploy.source}-${deploy.at}-${i}`} className="px-3 py-2 text-[12.5px]">
                <div className="flex items-center gap-2">
                  <span className="truncate font-medium">{deploy.title ?? deploy.source}</span>
                  {deploy.status && <span className="text-muted-foreground">{deploy.status}</span>}
                  {deploy.at && <span className="ml-auto shrink-0 text-subtle">{formatRelative(deploy.at)}</span>}
                </div>
                <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
                  <span>{deploy.source}</span>
                  {deploy.ref && <span className="font-mono">{deploy.ref}</span>}
                  {deploy.url && (
                    <a href={deploy.url} target="_blank" rel="noreferrer" className="underline-offset-2 hover:underline">
                      Open
                    </a>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
