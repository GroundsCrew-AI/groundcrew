"use client";

import { SettingsIllustration } from "@/components/states/illustrations";
import { PlaceholderPage } from "@/components/states/placeholder";
import { Skeleton } from "@/components/ui/skeleton";
import { useWorkspace } from "@/lib/workspace";

export default function SettingsPage() {
  const { workspace, me } = useWorkspace();
  return (
    <PlaceholderPage
      title="Settings"
      illustration={<SettingsIllustration />}
      heading="Workspace settings"
      description="Members, budgets, and the rules for what needs approval will live here."
      points={[
        "Invite teammates and choose who can approve write and emergency actions",
        "Daily token budget and the Ultra call limit per incident",
        "Approval rules: which actions may run without a human, if any",
      ]}
      phase="Arrives with hosting in Phase 6."
    >
      <dl className="mt-10 grid max-w-md grid-cols-[120px_minmax(0,1fr)] gap-y-2 border-t pt-5 text-[13px]">
        <dt className="text-muted-foreground">Workspace</dt>
        <dd className="truncate">{workspace?.name ?? <Skeleton className="h-4 w-32" />}</dd>
        <dt className="text-muted-foreground">Your role</dt>
        <dd>{workspace ? (workspace.role === "owner" ? "Owner" : "Member") : <Skeleton className="h-4 w-16" />}</dd>
        <dt className="text-muted-foreground">Signed in as</dt>
        <dd className="truncate">{me?.user.github_login ? `@${me.user.github_login}` : <Skeleton className="h-4 w-24" />}</dd>
      </dl>
    </PlaceholderPage>
  );
}
