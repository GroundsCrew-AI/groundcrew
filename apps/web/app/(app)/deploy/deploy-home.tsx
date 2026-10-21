"use client";

import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { DeployForm } from "@/components/deploy/deploy-form";
import { DeployList, DeployListHeader } from "@/components/deploy/deploy-list";
import { PageBody, PageHeader } from "@/components/shell/page-header";
import { DeployIllustration } from "@/components/states/illustrations";
import { EmptyState, ErrorState, RowsSkeleton } from "@/components/states/states";
import { Skeleton } from "@/components/ui/skeleton";
import { useConnections } from "@/lib/discovery";
import { useCreateDeploy, useDeploys } from "@/lib/deploys";
import { useWorkspace } from "@/lib/workspace";
import { canAct, VIEWER_READ_ONLY } from "@/lib/workspace-admin";

export function DeployHome() {
  const router = useRouter();
  const { workspace, error: workspaceError, refetch } = useWorkspace();
  const connections = useConnections(workspace?.id);
  const deploys = useDeploys(workspace?.id);
  const create = useCreateDeploy();
  const items = deploys.data?.items ?? [];
  const setupError = workspaceError ?? connections.error;

  return (
    <>
      <PageHeader title="Deploy" />
      <PageBody className="max-w-5xl space-y-10">
        <section aria-labelledby="new-deploy" className="space-y-4">
          <h2 id="new-deploy" className="text-[15px] font-medium">New deploy</h2>
          {setupError ? (
            <ErrorState
              error={setupError}
              onRetry={() => (workspaceError ? refetch() : void connections.refetch())}
            />
          ) : !workspace || connections.isLoading ? (
            <div className="space-y-3" aria-busy="true" aria-label="Loading connections">
              <Skeleton className="h-4 w-2/3" />
              <Skeleton className="h-9 w-full" />
              <Skeleton className="h-16 w-full" />
            </div>
          ) : !canAct(workspace.role) ? (
            <p className="text-[13px] text-muted-foreground">{VIEWER_READ_ONLY}</p>
          ) : (
            <DeployForm
              key={workspace.id}
              workspaceId={workspace.id}
              connections={connections.data ?? []}
              submitting={create.isPending}
              onSubmit={(body) =>
                create.mutate(body, {
                  onSuccess: (created) => {
                    toast.success("Deploy started");
                    router.push(`/deploy/${created.deploy_id}`);
                  },
                  onError: (error) => toast.error(error.message),
                })
              }
            />
          )}
        </section>

        <section aria-labelledby="deploys" className="space-y-3">
          <h2 id="deploys" className="text-[15px] font-medium">Deploys</h2>
          {deploys.error ? (
            <ErrorState error={deploys.error} onRetry={() => void deploys.refetch()} />
          ) : deploys.isLoading || !workspace ? (
            <RowsSkeleton rows={3} />
          ) : items.length === 0 ? (
            <EmptyState illustration={<DeployIllustration />} title="No deploys yet">
              Each deploy is one run: Scout, Builder, then Deployer, with your approval before anything changes.
            </EmptyState>
          ) : (
            <div className="-mx-4 md:mx-0 md:rounded-lg md:border">
              <DeployListHeader />
              <DeployList deploys={items} />
            </div>
          )}
        </section>
      </PageBody>
    </>
  );
}
