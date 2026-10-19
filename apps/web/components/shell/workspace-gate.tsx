"use client";

import { Plus, RotateCw } from "lucide-react";
import { usePathname } from "next/navigation";
import { type ReactNode, useState } from "react";

import { CreateWorkspaceDialog } from "@/components/settings/create-workspace-dialog";
import { PageBody, PageHeader } from "@/components/shell/page-header";
import { SettingsIllustration } from "@/components/states/illustrations";
import { EmptyState } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { useWorkspace } from "@/lib/workspace";

/**
 * Pages wait for a workspace before they load anything. Someone who belongs to none (removed
 * from their last team, or their workspace was deleted) would otherwise look at skeletons
 * forever, so the app says so plainly instead of rendering the page. Settings stays reachable,
 * since that is where workspaces are managed.
 */
export function WorkspaceGate({ children }: { children: ReactNode }) {
  const { me, workspaces, isLoading, error } = useWorkspace();
  const pathname = usePathname();
  const managing = pathname === "/settings" || pathname.startsWith("/settings/");
  if (me && !isLoading && !error && workspaces.length === 0 && !managing) return <NoWorkspace />;
  return children;
}

export function NoWorkspace() {
  const { me, refetch } = useWorkspace();
  const [creating, setCreating] = useState(false);
  const login = me?.user.github_login;
  const email = me?.user.email;
  return (
    <>
      <PageHeader title="No workspace" />
      <PageBody className="max-w-3xl">
        <EmptyState
          illustration={<SettingsIllustration />}
          title="You are not in a workspace"
          action={
            <div className="flex flex-wrap justify-center gap-2">
              <Button size="sm" onClick={() => setCreating(true)}>
                <Plus aria-hidden="true" />
                New workspace
              </Button>
              <Button size="sm" variant="outline" onClick={refetch}>
                <RotateCw aria-hidden="true" />
                Check again
              </Button>
            </div>
          }
        >
          Connections, runs, and incidents all belong to a workspace. Create your own, or ask a workspace owner to
          add you
          {email ? (
            <>
              {" "}
              by your email, <span className="font-mono text-foreground">{email}</span>
            </>
          ) : login ? (
            <>
              {" "}
              by your GitHub login, <span className="font-mono text-foreground">@{login}</span>
            </>
          ) : null}
          . You will see it here as soon as you are added.
        </EmptyState>
        {creating && <CreateWorkspaceDialog open onOpenChange={setCreating} />}
      </PageBody>
    </>
  );
}
