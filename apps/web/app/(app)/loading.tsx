import { RowsSkeleton } from "@/components/states/states";
import { Skeleton } from "@/components/ui/skeleton";

/** Shown while a page's server part loads on navigation: the shape of a page, not a spinner. */
export default function Loading() {
  return (
    <div aria-busy="true" aria-label="Loading page" className="flex flex-1 flex-col">
      <div className="flex h-12 shrink-0 items-center border-b px-4 md:px-6">
        <Skeleton className="h-4 w-28" />
      </div>
      <div className="max-w-5xl space-y-4 px-4 py-6 md:px-6">
        <Skeleton className="h-4 w-64 max-w-full" />
        <div className="overflow-hidden rounded-lg border">
          <RowsSkeleton rows={6} />
        </div>
      </div>
    </div>
  );
}
