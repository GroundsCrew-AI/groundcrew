import { cn } from "cn";
import { RotateCw } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

export function EmptyState({
  illustration,
  title,
  children,
  action,
  className,
}: {
  illustration?: ReactNode;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex max-w-md flex-col items-start gap-3 py-10", className)}>
      {illustration}
      <div className="space-y-1">
        <h2 className="text-[14px] font-medium">{title}</h2>
        {children && <div className="text-muted-foreground">{children}</div>}
      </div>
      {action}
    </div>
  );
}

export function ErrorState({
  error,
  onRetry,
  className,
}: {
  error: Error | null;
  onRetry?: () => void;
  className?: string;
}) {
  return (
    <div
      role="alert"
      className={cn(
        "flex flex-wrap items-center justify-between gap-3 rounded-lg border border-[color-mix(in_oklab,var(--status-failed)_35%,var(--border))] px-4 py-3",
        className,
      )}
    >
      <div>
        <p className="font-medium">Could not load this</p>
        <p className="text-muted-foreground">{error?.message ?? "Something went wrong."}</p>
      </div>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          <RotateCw aria-hidden="true" />
          Try again
        </Button>
      )}
    </div>
  );
}

export function RowsSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div aria-busy="true" aria-label="Loading" className="divide-y divide-border">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex items-center gap-4 px-4 py-3">
          <Skeleton className="h-4 w-20" />
          <Skeleton className="h-4 flex-1" style={{ maxWidth: `${60 - i * 6}%` }} />
          <Skeleton className="h-4 w-14" />
        </div>
      ))}
    </div>
  );
}
