"use client";

import { RotateCw } from "lucide-react";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

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
  const login = me?.user.github_login;
  return (
    <>
      <PageHeader title="No workspace" />
      <PageBody className="max-w-3xl">
        <EmptyState
          illustration={<SettingsIllustration />}
          title="You are not in a workspace"
          action={
            <Button size="sm" variant="outline" onClick={refetch}>
              <RotateCw aria-hidden="true" />
              Check again
            </Button>
          }
        >
          Connections, runs, and incidents all belong to a workspace. Ask a workspace owner to add you
          {login ? (
            <>
              {" "}
              by your GitHub login, <span className="font-mono text-foreground">@{login}</span>
            </>
          ) : null}
          . You will see it here as soon as you are added.
        </EmptyState>
      </PageBody>
    </>
  );
}
