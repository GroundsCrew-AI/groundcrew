"use client";

import { UserPlus, X } from "lucide-react";
import { type FormEvent, useState } from "react";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { UserAvatar } from "@/components/user-avatar";
import { formatRelative, formatTimestamp } from "@/lib/format";
import {
  type Invite,
  type Member,
  type Members,
  ownerCount,
  parseInvitee,
  ROLE_DESCRIPTION,
  ROLES,
  roleLabel,
  useAddMember,
  useCancelInvite,
  useRemoveMember,
  useUpdateMemberRole,
  withArticle,
  type WorkspaceRole,
} from "@/lib/workspace-admin";

import { toastResult } from "./toast-result";

function memberName(member: Member): string {
  return member.name || member.github_login || member.email || "Unknown user";
}

function inviteLabel(invite: Invite): string {
  return invite.email ?? `@${invite.github_login}`;
}

function RoleSelect({
  id,
  label,
  value,
  disabled,
  onChange,
}: {
  id?: string;
  label?: string;
  value: WorkspaceRole;
  disabled?: boolean;
  onChange: (role: WorkspaceRole) => void;
}) {
  return (
    <Select value={value} onValueChange={(v) => onChange(v as WorkspaceRole)} disabled={disabled}>
      <SelectTrigger id={id} aria-label={label} size="sm" className="w-28">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {ROLES.map((r) => (
          <SelectItem key={r} value={r}>
            {roleLabel(r)}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function MemberRow({
  member,
  workspaceId,
  workspaceName,
  isMe,
  onlyOwner,
  canManage,
}: {
  member: Member;
  workspaceId: string;
  workspaceName: string;
  isMe: boolean;
  onlyOwner: boolean;
  canManage: boolean;
}) {
  const updateRole = useUpdateMemberRole(workspaceId);
  const remove = useRemoveMember(workspaceId);
  // The lower role you picked for yourself, waiting for confirmation.
  const [demoteTo, setDemoteTo] = useState<WorkspaceRole | null>(null);
  const name = memberName(member);

  function changeRole(role: WorkspaceRole) {
    if (role === member.role) return;
    void toastResult(
      updateRole.mutateAsync({ userId: member.user_id, role }),
      () => `${name} is now ${withArticle(role)}`,
    );
  }

  return (
    <li className="flex flex-wrap items-center gap-3 px-4 py-3">
      <UserAvatar src={member.avatar_url} name={name} className="size-7" />
      <div className="min-w-0 flex-1">
        <p className="truncate text-[13px] font-medium">
          {name}
          {isMe && <span className="ml-1.5 font-normal text-muted-foreground">(you)</span>}
        </p>
        <p className="truncate text-[12px] text-muted-foreground">
          {member.github_login ? (
            <>@{member.github_login} · </>
          ) : member.email && member.name ? (
            <>{member.email} · </>
          ) : null}
          <time dateTime={member.joined_at} title={formatTimestamp(member.joined_at)}>
            joined {formatRelative(member.joined_at)}
          </time>
        </p>
      </div>
      {canManage && !onlyOwner ? (
        <RoleSelect
          label={`Role for ${name}`}
          value={member.role}
          disabled={updateRole.isPending}
          onChange={(role) => (isMe && role !== "owner" ? setDemoteTo(role) : changeRole(role))}
        />
      ) : (
        <span className="text-[12.5px] text-muted-foreground">
          {roleLabel(member.role)}
          {onlyOwner && <span className="text-subtle">, only owner</span>}
        </span>
      )}
      {canManage && !isMe && (
        <AlertDialog>
          <AlertDialogTrigger asChild>
            <Button size="icon-sm" variant="ghost" aria-label={`Remove ${name}`} disabled={remove.isPending}>
              <X />
            </Button>
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Remove {name}?</AlertDialogTitle>
              <AlertDialogDescription>
                They lose access to {workspaceName} at once. Runs and approvals they made stay in the history.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction
                variant="destructive-solid"
                onClick={() => void toastResult(remove.mutateAsync(member.user_id), () => `Removed ${name}`)}
              >
                Remove
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      )}
      <AlertDialog open={demoteTo !== null} onOpenChange={(open) => !open && setDemoteTo(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Make yourself {demoteTo ? withArticle(demoteTo) : ""}?</AlertDialogTitle>
            <AlertDialogDescription>
              {demoteTo ? ROLE_DESCRIPTION[demoteTo] : ""} You will no longer be able to change connections, members,
              or settings in {workspaceName}. Another owner can make you an owner again.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={() => demoteTo && changeRole(demoteTo)}>
              Make me {demoteTo ? withArticle(demoteTo) : ""}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </li>
  );
}

function AddMemberForm({ workspaceId }: { workspaceId: string }) {
  const add = useAddMember(workspaceId);
  const [who, setWho] = useState("");
  const [role, setRole] = useState<WorkspaceRole>("viewer");
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const target = parseInvitee(who);
    if (!target) {
      setError("Enter an email address (ada@example.com) or a GitHub login (octocat).");
      return;
    }
    setError(null);
    const label = "email" in target ? target.email : `@${target.github_login}`;
    const added = await toastResult(add.mutateAsync({ ...target, role }), (members) => {
      const joined = members.members.some((m) =>
        "email" in target
          ? m.email?.toLowerCase() === target.email
          : m.github_login?.toLowerCase() === target.github_login.toLowerCase(),
      );
      if (joined) return `${label} joined as ${withArticle(role)}`;
      return "email" in target
        ? `Invited ${label}. We emailed them a link; they join once they confirm the address.`
        : `Invited ${label}. They join when they sign in with that GitHub account.`;
    });
    if (added) setWho("");
  }

  return (
    <form onSubmit={submit} className="space-y-2" noValidate>
      <h3 className="text-[13px] font-medium">Add a member</h3>
      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-56 flex-1 space-y-1.5">
          <Label htmlFor="member-login" className="text-[12px] font-normal text-muted-foreground">
            Email or GitHub login
          </Label>
          <Input
            id="member-login"
            value={who}
            onChange={(e) => setWho(e.target.value)}
            placeholder="ada@example.com or octocat"
            autoComplete="off"
            spellCheck={false}
            maxLength={320}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? "member-login-error" : "member-login-hint"}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="member-role" className="text-[12px] font-normal text-muted-foreground">
            Role
          </Label>
          <RoleSelect id="member-role" value={role} onChange={setRole} />
        </div>
        <Button type="submit" size="sm" className="h-8" disabled={!who.trim() || add.isPending}>
          <UserPlus aria-hidden="true" />
          {add.isPending ? "Adding" : "Add"}
        </Button>
      </div>
      {error ? (
        <p id="member-login-error" role="alert" className="text-[12px] text-[var(--status-failed)]">
          {error}
        </p>
      ) : (
        <p id="member-login-hint" className="text-[12px] text-muted-foreground">
          People with a Groundcrew account join at once. Anyone else gets an invite: by email for an address, or
          when they first sign in with that GitHub account.
        </p>
      )}
    </form>
  );
}

function InviteRow({ invite, workspaceId, canManage }: { invite: Invite; workspaceId: string; canManage: boolean }) {
  const cancel = useCancelInvite(workspaceId);
  return (
    <li className="flex flex-wrap items-center gap-3 px-4 py-2.5">
      <span className="min-w-0 flex-1 truncate text-[13px]">{inviteLabel(invite)}</span>
      <span className="text-[12px] text-muted-foreground">
        {roleLabel(invite.role)} ·{" "}
        <time dateTime={invite.created_at} title={formatTimestamp(invite.created_at)}>
          invited {formatRelative(invite.created_at)}
        </time>
      </span>
      {canManage && (
        <Button
          size="sm"
          variant="ghost"
          disabled={cancel.isPending}
          aria-label={`Cancel the invite for ${inviteLabel(invite)}`}
          onClick={() =>
            void toastResult(cancel.mutateAsync(invite.id), () => `Cancelled the invite for ${inviteLabel(invite)}`)
          }
        >
          Cancel invite
        </Button>
      )}
    </li>
  );
}

/** Members with their roles, adding people by email or GitHub login, and invites not used yet. */
export function MembersSection({
  workspaceId,
  workspaceName,
  members,
  meId,
  canManage,
}: {
  workspaceId: string;
  workspaceName: string;
  members: Members;
  meId: string | undefined;
  canManage: boolean;
}) {
  const owners = ownerCount(members.members);
  return (
    <div className="space-y-6">
      <ul aria-label="Members" className="divide-y overflow-hidden rounded-lg border">
        {members.members.map((member) => (
          <MemberRow
            key={member.user_id}
            member={member}
            workspaceId={workspaceId}
            workspaceName={workspaceName}
            isMe={member.user_id === meId}
            onlyOwner={member.role === "owner" && owners === 1}
            canManage={canManage}
          />
        ))}
      </ul>

      {canManage && <AddMemberForm workspaceId={workspaceId} />}

      {members.invites.length > 0 && (
        <section aria-labelledby="invites-title" className="space-y-2">
          <h3 id="invites-title" className="text-[13px] font-medium">
            Pending invites
          </h3>
          <p className="text-[12px] text-muted-foreground">
            They join when they confirm their email address, or sign in with that GitHub account.
          </p>
          <ul aria-label="Pending invites" className="divide-y overflow-hidden rounded-lg border">
            {members.invites.map((invite) => (
              <InviteRow key={invite.id} invite={invite} workspaceId={workspaceId} canManage={canManage} />
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
