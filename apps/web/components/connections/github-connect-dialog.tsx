"use client";

import { ExternalLink, Lock } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { useGitHubInstallations, useLinkGitHub } from "@/lib/discovery";
import { useWorkspace } from "@/lib/workspace";

function Problem({ children, onRetry }: { children: React.ReactNode; onRetry: () => void }) {
  return (
    <div role="alert" className="space-y-2">
      <p className="text-[12.5px] text-[var(--status-failed)]">{children}</p>
      <Button size="sm" variant="outline" onClick={onRetry}>
        Try again
      </Button>
    </div>
  );
}

/** Pick repositories the Groundcrew GitHub App can see and link them to this workspace. */
export function GitHubConnectDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const { workspace } = useWorkspace();
  const installations = useGitHubInstallations(workspace?.id, open);
  const link = useLinkGitHub();
  const [picked, setPicked] = useState<Record<number, Set<string>>>({});

  const data = installations.data;
  function selected(installationId: number): Set<string> {
    const linked = data?.installations
      .find((i) => i.id === installationId)
      ?.repos.filter((r) => r.linked)
      .map((r) => r.full_name);
    return picked[installationId] ?? new Set(linked ?? []);
  }

  function toggle(installationId: number, repo: string, on: boolean) {
    const next = new Set(selected(installationId));
    if (on) next.add(repo);
    else next.delete(repo);
    setPicked({ ...picked, [installationId]: next });
  }

  function save(installationId: number) {
    if (!workspace) return;
    const repos = [...selected(installationId)];
    link.mutate(
      { workspace_id: workspace.id, installation_id: installationId, repos },
      {
        onSuccess: () => {
          toast.success(`Linked ${repos.length} ${repos.length === 1 ? "repository" : "repositories"}`);
          onOpenChange(false);
        },
        onError: (error) => toast.error(error.message),
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Link GitHub repositories</DialogTitle>
          <DialogDescription>
            Groundcrew reads Terraform, Dockerfiles, workflows, recent runs, and commits through its GitHub App,
            with short-lived installation tokens.
          </DialogDescription>
        </DialogHeader>
        {installations.error ? (
          <Problem onRetry={() => void installations.refetch()}>{installations.error.message}</Problem>
        ) : !data ? (
          <div className="space-y-2">
            <Skeleton className="h-4 w-40" />
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
        ) : !data.configured ? (
          <p className="rounded-md border border-dashed px-3 py-2.5 text-[12.5px] text-muted-foreground">
            GitHub is not set up on this Groundcrew server yet. An admin adds the Groundcrew GitHub App&apos;s ID
            and private key (<span className="font-mono">GITHUB_APP_ID</span>,{" "}
            <span className="font-mono">GITHUB_APP_PRIVATE_KEY_PATH</span>; see docs/self-hosting.md).
          </p>
        ) : data.error ? (
          <Problem onRetry={() => void installations.refetch()}>
            Could not list the repositories the GitHub App can see. {data.error}
          </Problem>
        ) : data.installations.length === 0 ? (
          <div className="space-y-3 rounded-md border border-dashed px-4 py-4">
            <p className="text-[13px]">
              First, install the Groundcrew GitHub App and choose the repositories it may read. Then come back
              here and pick them.
            </p>
            <div className="flex flex-wrap gap-2">
              {data.install_url && (
                <Button asChild size="sm">
                  <a href={data.install_url} target="_blank" rel="noreferrer">
                    Install the GitHub App
                    <ExternalLink aria-hidden="true" />
                  </a>
                </Button>
              )}
              <Button size="sm" variant="outline" onClick={() => void installations.refetch()}>
                {installations.isFetching ? "Checking" : "I installed it"}
              </Button>
            </div>
          </div>
        ) : (
          <div className="space-y-5">
            {data.installations.map((installation) => {
              const chosen = selected(installation.id);
              return (
                <fieldset key={installation.id} className="space-y-2">
                  <legend className="mb-1 text-[12px] font-medium text-muted-foreground">{installation.account}</legend>
                  {installation.repos.length === 0 && (
                    <p className="rounded-lg border px-3 py-2 text-[12.5px] text-muted-foreground">
                      No repositories are shared with the app on this account yet. Use the link below to add some.
                    </p>
                  )}
                  <ul className="divide-y rounded-lg border empty:hidden">
                    {installation.repos.map((repo) => (
                      <li key={repo.full_name}>
                        <label className="flex cursor-pointer items-center gap-3 px-3 py-2 hover:bg-accent/50">
                          <input
                            type="checkbox"
                            className="size-3.5 accent-[var(--primary)]"
                            checked={chosen.has(repo.full_name)}
                            onChange={(e) => toggle(installation.id, repo.full_name, e.target.checked)}
                          />
                          <span className="min-w-0 flex-1 truncate font-mono text-[12px]">{repo.full_name}</span>
                          {repo.private && <Lock aria-label="Private" className="size-3 text-subtle" />}
                          {repo.linked && <span className="text-[11px] text-subtle">Linked</span>}
                        </label>
                      </li>
                    ))}
                  </ul>
                  <div className="flex justify-end">
                    <Button size="sm" disabled={chosen.size === 0 || link.isPending} onClick={() => save(installation.id)}>
                      {link.isPending ? "Linking" : `Link ${chosen.size} ${chosen.size === 1 ? "repository" : "repositories"}`}
                    </Button>
                  </div>
                </fieldset>
              );
            })}
          </div>
        )}
        {data?.install_url && data.installations.length > 0 && (
          <DialogFooter className="sm:justify-start">
            <a
              href={data.install_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1.5 text-[12.5px] text-muted-foreground underline-offset-2 hover:underline"
            >
              Install the app on more repositories
              <ExternalLink aria-hidden="true" className="size-3" />
            </a>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  );
}
