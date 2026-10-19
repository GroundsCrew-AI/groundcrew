"use client";

import Link from "next/link";

import { NewRunDialog } from "@/components/runs/new-run-dialog";
import { RunList, RunListHeader } from "@/components/runs/run-list";
import { PageBody, PageHeader } from "@/components/shell/page-header";
import { RunsIllustration } from "@/components/states/illustrations";
import { EmptyState, ErrorState, RowsSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { useRuns } from "@/lib/queries";
import { useWorkspace } from "@/lib/workspace";

export default function RunsPage() {
  const { workspace, error: workspaceError, refetch } = useWorkspace();
  const runs = useRuns(workspace?.id);
  const items = runs.data?.pages.flatMap((page) => page.items) ?? [];
  const error = workspaceError ?? runs.error;

  return (
    <>
      <PageHeader title="Runs" actions={<NewRunDialog />} />
      <PageBody className="p-0 md:p-0">
        {error ? (
          <div className="p-4 md:p-6">
            <ErrorState error={error} onRetry={() => (workspaceError ? refetch() : void runs.refetch())} />
          </div>
        ) : runs.isLoading || !workspace ? (
          <RowsSkeleton rows={6} />
        ) : items.length === 0 ? (
          <div className="px-4 md:px-6">
            <EmptyState
              illustration={<RunsIllustration />}
              title="No runs yet"
              action={
                <Button size="sm" asChild>
                  <Link href="/connections">Go to Connections</Link>
                </Button>
              }
            >
              A run is one agent working on one goal: discovering your infrastructure, deploying an app, or
              investigating an incident. Every step shows up here as it happens. Connect an account and run
              discovery to start the first one.
            </EmptyState>
          </div>
        ) : (
          <>
            <RunListHeader />
            <RunList runs={items} />
            {runs.hasNextPage && (
              <div className="border-t px-4 py-3">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => void runs.fetchNextPage()}
                  disabled={runs.isFetchingNextPage}
                >
                  {runs.isFetchingNextPage ? "Loading" : "Load older runs"}
                </Button>
              </div>
            )}
          </>
        )}
      </PageBody>
    </>
  );
}
