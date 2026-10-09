"use client";

import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import { toast } from "sonner";

import { AwsConnectDialog, openPendingTab } from "@/components/connections/aws-connect-dialog";
import { ConnectionRow } from "@/components/connections/connection-row";
import { GitHubConnectDialog } from "@/components/connections/github-connect-dialog";
import { RunDiscoveryButton } from "@/components/connections/run-discovery-button";
import {
  AzureConnectDialog,
  KubernetesConnectDialog,
  VercelConnectDialog,
} from "@/components/connections/token-connect-dialogs";
import { ProviderMark } from "@/components/map/resource-meta";
import { PageBody, PageHeader } from "@/components/shell/page-header";
import { ErrorState, RowsSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { type AwsTier, type Connection, type ConnectionKind, useConnections, useRefreshConnection } from "@/lib/discovery";
import { useWorkspace } from "@/lib/workspace";

type Provider = {
  kind: ConnectionKind | "vm";
  name: string;
  access: string;
  action: string;
  preview?: boolean;
  later?: boolean;
};

const PROVIDERS: Provider[] = [
  { kind: "aws", name: "AWS", access: "A role with your ExternalId. No stored keys. Observe reads; Deploy also deploys into its own cluster.", action: "Connect AWS" },
  { kind: "github", name: "GitHub", access: "The Groundcrew GitHub App on the repositories you pick.", action: "Pick repositories" },
  { kind: "vercel", name: "Vercel", access: "A team token, stored encrypted. Env var names, not values.", action: "Add token" },
  { kind: "azure", name: "Azure", access: "Reader on one resource group, through a service principal.", action: "Connect", preview: true },
  { kind: "k8s", name: "Kubernetes", access: "A service account's kubeconfig, checked before saving. Groundcrew never lists Secrets.", action: "Upload kubeconfig" },
  { kind: "vm", name: "VM", access: "SSH key, encrypted at rest. Read commands only.", action: "Connect", later: true },
];

export default function ConnectionsPage() {
  const router = useRouter();
  const { workspace, error: workspaceError, refetch } = useWorkspace();
  const connections = useConnections(workspace?.id);
  const refresh = useRefreshConnection();
  const [open, setOpen] = useState<ConnectionKind | null>(null);
  const [awsTier, setAwsTier] = useState<AwsTier>("observe");
  const awsTab = useRef<Window | null>(null);
  const error = workspaceError ?? connections.error;
  const items = connections.data ?? [];

  function onRefresh(connection: Connection) {
    refresh.mutate(connection, {
      onSuccess: (run) => router.push(`/runs/${run.id}`),
      onError: (e) => toast.error(e.message),
    });
  }

  function openDialog(kind: ConnectionKind) {
    // AWS opens CloudFormation in a new tab; the tab must open in the click itself.
    if (kind === "aws") awsTab.current = openPendingTab();
    setOpen(kind);
  }

  const dialog = (kind: ConnectionKind) => ({
    open: open === kind,
    onOpenChange: (next: boolean) => setOpen(next ? kind : null),
  });

  return (
    <>
      <PageHeader title="Connections" actions={items.some((c) => c.status === "connected") && <RunDiscoveryButton />} />
      <PageBody className="max-w-4xl space-y-8">
        <div className="max-w-[62ch]">
          <h2 className="text-[17px] font-semibold tracking-tight">Connect the infrastructure you already have</h2>
          <p className="mt-2 text-[13.5px] leading-relaxed text-muted-foreground">
            Groundcrew reads with scoped, read-only access and never reads secret values or your data. Anything
            that changes a resource goes through an approval or a pull request.
          </p>
        </div>

        {error ? (
          <ErrorState error={error} onRetry={() => (workspaceError ? refetch() : void connections.refetch())} />
        ) : connections.isLoading || !workspace ? (
          <div className="overflow-hidden rounded-lg border">
            <RowsSkeleton rows={3} />
          </div>
        ) : (
          items.length > 0 && (
            <section aria-labelledby="connected-title" className="space-y-2">
              <h2 id="connected-title" className="text-[12px] font-medium text-muted-foreground">
                Connected
              </h2>
              <ul className="divide-y overflow-hidden rounded-lg border">
                {items.map((connection) => (
                  <ConnectionRow
                    key={connection.id}
                    connection={connection}
                    onRefresh={onRefresh}
                    refreshing={refresh.isPending && refresh.variables?.id === connection.id}
                    onResume={(conn) => {
                      setAwsTier(conn.tier ?? "observe");
                      openDialog("aws");
                    }}
                  />
                ))}
              </ul>
            </section>
          )
        )}

        <section aria-labelledby="add-title" className="space-y-2">
          <h2 id="add-title" className="text-[12px] font-medium text-muted-foreground">
            Add a connection
          </h2>
          <ul className="grid gap-px overflow-hidden rounded-lg border bg-border sm:grid-cols-2">
            {PROVIDERS.map((provider) => (
              <li key={provider.kind} className="flex gap-3 bg-background px-4 py-4">
                <ProviderMark provider={provider.kind} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center justify-between gap-2">
                    <h3 className="font-medium">{provider.name}</h3>
                    {provider.preview && <span className="text-[11.5px] text-subtle">Preview</span>}
                    {provider.later && <span className="text-[11.5px] text-subtle">Later</span>}
                  </div>
                  <p className="mt-0.5 text-[12.5px] text-muted-foreground">{provider.access}</p>
                  {provider.kind === "aws" && (
                    <fieldset className="mt-3 flex gap-1 rounded-md border p-0.5 text-[12px]">
                      <legend className="sr-only">AWS access</legend>
                      {(["observe", "deploy"] as const).map((tier) => (
                        <label
                          key={tier}
                          className={`flex-1 cursor-pointer rounded px-2 py-1 text-center ${awsTier === tier ? "bg-muted font-medium" : "text-muted-foreground"}`}
                        >
                          <input
                            type="radio"
                            name="aws-tier"
                            value={tier}
                            checked={awsTier === tier}
                            onChange={() => setAwsTier(tier)}
                            className="sr-only"
                          />
                          {tier === "observe" ? "Observe" : "Deploy"}
                        </label>
                      ))}
                    </fieldset>
                  )}
                  {!provider.later && (
                    <Button
                      size="sm"
                      variant="outline"
                      className="mt-3"
                      disabled={!workspace}
                      onClick={() => openDialog(provider.kind as ConnectionKind)}
                    >
                      {provider.action}
                    </Button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </section>
      </PageBody>
      <AwsConnectDialog {...dialog("aws")} tab={awsTab} tier={awsTier} />
      <GitHubConnectDialog {...dialog("github")} />
      <VercelConnectDialog {...dialog("vercel")} />
      <AzureConnectDialog {...dialog("azure")} />
      <KubernetesConnectDialog {...dialog("k8s")} />
    </>
  );
}
