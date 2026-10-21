"use client";

import Link from "next/link";

import { ErrorState, RowsSkeleton } from "@/components/states/states";
import { useConnections } from "@/lib/discovery";

/** For each connection, what Groundcrew can read and change through it. */
export function AccessSection({ workspaceId }: { workspaceId: string }) {
  const connections = useConnections(workspaceId);
  if (connections.error) return <ErrorState error={connections.error} onRetry={() => void connections.refetch()} />;
  if (!connections.data) {
    return (
      <div className="overflow-hidden rounded-lg border">
        <RowsSkeleton rows={2} />
      </div>
    );
  }
  if (connections.data.length === 0) {
    return (
      <p className="text-[12.5px] text-muted-foreground">
        Nothing is connected yet. <Link href="/connections" className="underline underline-offset-4">Connect an account</Link>.
      </p>
    );
  }
  return (
    <ul className="divide-y overflow-hidden rounded-lg border">
      {connections.data.map((c) => (
        <li key={c.id} className="space-y-1.5 px-4 py-3">
          <p className="text-[13px] font-medium">
            {c.name}
            {c.account && <span className="ml-1.5 font-normal text-muted-foreground">{c.account}</span>}
          </p>
          <ul className="list-disc space-y-1 pl-5 text-[12.5px] text-muted-foreground">
            {(c.access ?? []).map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </li>
      ))}
    </ul>
  );
}
