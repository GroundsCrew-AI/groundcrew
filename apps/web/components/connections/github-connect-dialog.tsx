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
          <p role="alert" className="text-[var(--status-failed)]">
            {installations.error.message}
          </p>
        ) : !data ? (
          <div className="space-y-2">
            <Skeleton className="h-4 w-40" />
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
        ) : !data.configured ? (
          <p className="text-muted-foreground">
            The GitHub App is not configured on this server (GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH).
          </p>
        ) : (
          <div className="space-y-5">
            {data.error && <p className="text-[12.5px] text-[var(--status-failed)]">{data.error}</p>}
            {data.installations.length === 0 && (
              <p className="text-muted-foreground">The app is not installed on any account yet.</p>
            )}
            {data.installations.map((installation) => {
              const chosen = selected(installation.id);
              return (
                <fieldset key={installation.id} className="space-y-2">
                  <legend className="mb-1 text-[12px] font-medium text-muted-foreground">{installation.account}</legend>
                  <ul className="divide-y rounded-lg border">
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
        {data?.install_url && (
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
