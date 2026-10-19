"use client";

import { Lock, Plus } from "lucide-react";
import { type ReactNode, useState } from "react";

import { AccountSection } from "@/components/settings/account-section";
import { LeaveWorkspace } from "@/components/settings/leave-workspace";
import { MembersSection } from "@/components/settings/members-section";
import { SettingsSection } from "@/components/settings/settings-section";
import { WorkspaceNameForm, WorkspaceSettingsForm } from "@/components/settings/workspace-forms";
import { CreateWorkspaceDialog } from "@/components/settings/create-workspace-dialog";
import { PageBody, PageHeader } from "@/components/shell/page-header";
import { SettingsIllustration } from "@/components/states/illustrations";
import { EmptyState, ErrorState, RowsSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { formatTimestamp } from "@/lib/format";
import { useSummary } from "@/lib/queries";
import { useWorkspace } from "@/lib/workspace";
import { OWNER_ONLY, ownerCount, roleLabel, useMembers, useWorkspaceDetail } from "@/lib/workspace-admin";

function NoWorkspace() {
  const [open, setOpen] = useState(false);
  return (
    <EmptyState
      illustration={<SettingsIllustration />}
      title="You are not in a workspace"
      action={
        <Button size="sm" onClick={() => setOpen(true)}>
          <Plus aria-hidden="true" />
          New workspace
        </Button>
      }
    >
      Create one, or ask an owner to add your email address to theirs.
      <CreateWorkspaceDialog open={open} onOpenChange={setOpen} />
    </EmptyState>
  );
}

/** Workspace name and settings, members and invites, and leaving. Owners change things;
 * members see the same page read-only, with the reason. */
export default function SettingsPage() {
  const { workspace, me, isLoading, error: workspaceError, refetch } = useWorkspace();
  const detail = useWorkspaceDetail(workspace?.id);
  const members = useMembers(workspace?.id);
  const summary = useSummary(workspace?.id);

  const error = workspaceError ?? detail.error ?? members.error;
  const data = detail.data;
  const owner = (data?.role ?? workspace?.role) === "owner";
  const onlyOwner = owner && members.data !== undefined && ownerCount(members.data.members) === 1;
  // The summary's cap is the platform default while the workspace has none of its own.
  const platformCap = data && data.settings.daily_usd_cap == null ? (summary.data?.daily_usd_cap ?? null) : null;

  let body;
  if (error) {
    body = (
      <ErrorState
        error={error}
        onRetry={() => (workspaceError ? refetch() : (void detail.refetch(), void members.refetch()))}
      />
    );
  } else if (!isLoading && !workspace) {
    body = <NoWorkspace />;
  } else if (!workspace || !data) {
    body = (
      <div className="overflow-hidden rounded-lg border">
        <RowsSkeleton rows={4} />
      </div>
    );
  } else {
    body = (
      <>
        {!owner && (
          <p
            role="note"
            className="mb-8 flex items-start gap-2 rounded-lg border px-3 py-2.5 text-[12.5px] text-muted-foreground"
          >
            <Lock aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
            <span>
              You are a member of {data.name}. Owners change its name, settings, members, and connections. You can
              see everything here, run discovery, report incidents, and approve pull requests and deploys.
            </span>
          </p>
        )}

        <SettingsSection id="settings-workspace" title="Workspace" description={owner ? undefined : OWNER_ONLY.workspace}>
          <div className="space-y-4">
            <WorkspaceNameForm key={`${data.id}:${data.name}`} workspace={data} canEdit={owner} />
            <dl className="grid max-w-md grid-cols-[120px_minmax(0,1fr)] gap-y-1.5 text-[12.5px]">
              <dt className="text-muted-foreground">Your role</dt>
              <dd>{roleLabel(data.role)}</dd>
              <dt className="text-muted-foreground">Created</dt>
              <dd>
                <time dateTime={data.created_at}>{formatTimestamp(data.created_at)}</time>
              </dd>
            </dl>
          </div>
        </SettingsSection>

        <SettingsSection
          id="settings-rules"
          title="Approvals and limits"
          description={owner ? "Applies to everyone in this workspace." : OWNER_ONLY.workspace}
        >
          <WorkspaceSettingsForm
            key={`${data.id}:${JSON.stringify(data.settings)}`}
            workspace={data}
            canEdit={owner}
            platformCap={platformCap}
          />
        </SettingsSection>

        <SettingsSection
          id="settings-members"
          title="Members"
          description={
            owner
              ? "Owners manage connections, members, and settings. Members see everything and approve pull requests and deploys."
              : OWNER_ONLY.members
          }
        >
          {members.data ? (
            <MembersSection
              workspaceId={data.id}
              workspaceName={data.name}
              members={members.data}
              meId={me?.user.id}
              canManage={owner}
            />
          ) : (
            <div className="overflow-hidden rounded-lg border">
              <RowsSkeleton rows={2} />
            </div>
          )}
        </SettingsSection>

        {me && (
          <SettingsSection
            id="settings-leave"
            title="Leave workspace"
            description="Stop being a member of this workspace. Your other workspaces are not affected."
          >
            <LeaveWorkspace workspaceId={data.id} workspaceName={data.name} userId={me.user.id} onlyOwner={onlyOwner} />
          </SettingsSection>
        )}
      </>
    );
  }

  return (
    <>
      <PageHeader title="Settings" actions={<NewWorkspaceButton />} />
      <PageBody className="max-w-4xl space-y-12">
        {me && (
          <div>
            <GroupTitle>Account</GroupTitle>
            <AccountSection me={me} />
          </div>
        )}
        <div>
          <GroupTitle>{data ? `Workspace: ${data.name}` : "Workspace"}</GroupTitle>
          <div>{body}</div>
        </div>
      </PageBody>
    </>
  );
}

function GroupTitle({ children }: { children: ReactNode }) {
  return (
    <h2 className="mb-6 border-b pb-2 text-[12px] font-medium tracking-wide text-muted-foreground uppercase">
      {children}
    </h2>
  );
}

function NewWorkspaceButton() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button size="sm" variant="outline" onClick={() => setOpen(true)}>
        <Plus aria-hidden="true" />
        New workspace
      </Button>
      <CreateWorkspaceDialog open={open} onOpenChange={setOpen} />
    </>
  );
}
