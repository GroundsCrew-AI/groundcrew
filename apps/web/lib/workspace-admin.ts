"use client";

/**
 * Workspaces as teams: a workspace's name and settings, its members and invites, and the
 * permission checks the UI makes before the API would refuse.
 *
 * The API decides (a 403 with the reason, which callers show as a toast). These checks only
 * hide or disable what a role cannot do and say why, so nobody has to click to find out. They
 * mirror the API: viewers read only; approvers also start work and approve; connections,
 * members, and settings are for owners; emergency approvals need an owner unless the
 * workspace lets approvers approve them.
 */

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

import { api, type Approval, type Me, type Schemas, unwrap, type Workspace } from "@/lib/api/client";
import { meKey, useWorkspace } from "@/lib/workspace";

export type WorkspaceDetail = Schemas["WorkspaceDetail"];
export type WorkspaceSettings = Schemas["WorkspaceConfig"];
export type WorkspaceRole = Schemas["WorkspaceRole"];
export type Members = Schemas["MembersOut"];
export type Member = Schemas["MemberOut"];
export type Invite = Schemas["InviteOut"];
export type NotificationSettings = Schemas["NotificationConfig"];
export type ChannelTestResult = Schemas["ChannelTestOut"];
export type AuditEvent = Schemas["AuditEventOut"];
export type AuditPage = Schemas["AuditPage"];
export type Usage = Schemas["UsageOut"];

export const workspaceAdminKeys = {
  detail: (ws: string) => ["workspace", ws] as const,
  members: (ws: string) => ["members", ws] as const,
  audit: (ws: string) => ["audit", ws] as const,
  usage: (ws: string) => ["usage", ws] as const,
};

/** Why a member cannot do something. Shown next to the hidden or disabled control. */
export const OWNER_ONLY = {
  connections: "Only workspace owners can add, change, or remove connections.",
  emergency: "Only workspace owners can approve emergency actions.",
  workspace: "Only workspace owners can change the name and settings.",
  members: "Only workspace owners can add members, change roles, or remove people.",
  lastOwner: "A workspace needs at least one owner. Make someone else an owner first.",
} as const;

/** Why a viewer cannot start or decide anything (the API answers 403 with the same idea). */
export const VIEWER_READ_ONLY = "Viewers can look but not change anything. Ask a workspace owner for the approver role.";

/** Highest first, as the role pickers list them. */
export const ROLES: readonly WorkspaceRole[] = ["owner", "approver", "viewer"];

export const ROLE_DESCRIPTION: Record<WorkspaceRole, string> = {
  owner: "Owners manage members, connections, and settings, and approve everything.",
  approver: "Approvers start deploys and runs and approve deploys and fixes.",
  viewer: "Viewers see everything in the workspace and change nothing.",
};

export function roleLabel(role: WorkspaceRole): string {
  return role === "owner" ? "Owner" : role === "approver" ? "Approver" : "Viewer";
}

/** "an owner", "an approver", "a viewer". */
export function withArticle(role: WorkspaceRole): string {
  return `${role === "viewer" ? "a" : "an"} ${role}`;
}

/** True when this role can start runs, deploys, and incidents and decide approvals. */
export function canAct(role: WorkspaceRole | undefined): boolean {
  return role === "owner" || role === "approver";
}

/**
 * Why this person cannot approve an action, or null when they can. Mirrors the API check:
 * viewers never decide; an emergency action needs an owner unless the workspace setting lets
 * approvers approve it. An unknown role never blocks (the API still decides); unknown settings
 * count as the default, owners only.
 */
export function approveBlockedReason(
  policy: Approval["policy"],
  role: WorkspaceRole | undefined,
  emergencyApprovers: WorkspaceSettings["emergency_approvers"] | undefined,
): string | null {
  if (role === "viewer") return VIEWER_READ_ONLY;
  if (policy !== "emergency" || role !== "approver") return null;
  return (emergencyApprovers ?? "owners") === "approvers" ? null : OWNER_ONLY.emergency;
}

// ---------------------------------------------------------------------------- queries

export function useWorkspaceDetail(workspaceId: string | undefined) {
  return useQuery({
    queryKey: workspaceAdminKeys.detail(workspaceId ?? ""),
    enabled: Boolean(workspaceId),
    staleTime: 30_000,
    queryFn: async () =>
      unwrap(
        await api.GET("/workspaces/{workspace_id}", {
          params: { path: { workspace_id: workspaceId! } },
        }),
      ),
  });
}

export function useMembers(workspaceId: string | undefined) {
  return useQuery({
    queryKey: workspaceAdminKeys.members(workspaceId ?? ""),
    enabled: Boolean(workspaceId),
    queryFn: async () =>
      unwrap(
        await api.GET("/workspaces/{workspace_id}/members", {
          params: { path: { workspace_id: workspaceId! } },
        }),
      ),
  });
}

/** What the workspace used of its limits today (model spend, research, builds, runs). */
export function useUsage(workspaceId: string | undefined) {
  return useQuery({
    queryKey: workspaceAdminKeys.usage(workspaceId ?? ""),
    enabled: Boolean(workspaceId),
    refetchInterval: 60_000,
    queryFn: async () =>
      unwrap(
        await api.GET("/workspaces/{workspace_id}/usage", {
          params: { path: { workspace_id: workspaceId! } },
        }),
      ),
  });
}

/** The workspace's audit log, newest first, 50 at a time. */
export function useAuditLog(workspaceId: string | undefined) {
  return useInfiniteQuery({
    queryKey: workspaceAdminKeys.audit(workspaceId ?? ""),
    enabled: Boolean(workspaceId),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page: AuditPage) => page.next_before ?? undefined,
    queryFn: async ({ pageParam }): Promise<AuditPage> =>
      unwrap(
        await api.GET("/workspaces/{workspace_id}/audit", {
          params: { path: { workspace_id: workspaceId! }, query: pageParam ? { before: pageParam } : {} },
        }),
      ),
  });
}

const AUDIT_ACTIONS: Record<string, string> = {
  "approval.approved": "approved",
  "approval.rejected": "rejected",
  "member.added": "added a member",
  "member.removed": "removed a member",
  "member.left": "left the workspace",
  "member.role_changed": "changed a role",
  "invite.created": "invited",
  "invite.updated": "changed an invite",
  "invite.canceled": "canceled an invite",
  "workspace.created": "created the workspace",
  "workspace.updated": "changed the workspace",
  "connection.created": "added a connection",
  "connection.deleted": "removed a connection",
  "deploy.started": "started a deploy",
  "incident.reported": "reported an incident",
  "run.stopped": "stopped a run",
  "deploy.settings": "changed a deploy's settings",
  "token.created": "made an assistant token",
  "token.revoked": "revoked an assistant token",
  "assistant.start_deploy": "asked to deploy",
  "assistant.change_settings": "asked to change settings",
  "assistant.report_incident": "asked to report an incident",
  "assistant.retry_fix": "asked to retry a fix",
};

/** One line for an audit event: "approved target_apply (write)", "changed a role: viewer to approver". */
export function auditSummary(event: Pick<AuditEvent, "action" | "detail">): string {
  const verb = AUDIT_ACTIONS[event.action] ?? event.action.replace(/[._]/g, " ");
  const d = event.detail as Record<string, unknown>;
  const text = (key: string) => (typeof d[key] === "string" ? (d[key] as string) : undefined);
  if (event.action.startsWith("approval.")) return `${verb} ${text("tool") ?? "an action"} (${text("policy") ?? "?"})`;
  if (event.action === "member.role_changed") return `${verb}: ${text("from")} to ${text("to")}`;
  const who = text("email") ?? (text("github_login") ? `@${text("github_login")}` : undefined);
  if (who) return `${verb} ${who}${text("role") ? ` as ${text("role")}` : ""}`;
  if (event.action.startsWith("connection.")) {
    const what = text("name") ?? text("kind");
    return what ? `${verb}: ${what}` : verb;
  }
  if (event.action === "workspace.updated" && Array.isArray(d.changed)) return `${verb}: ${(d.changed as string[]).join(", ")}`;
  if (event.action === "deploy.started" && text("source")) return `${verb} of ${text("source")}`;
  if (event.action.startsWith("token.") && text("name")) return `${verb}: ${text("name")}`;
  if (event.action.startsWith("assistant.")) {
    // Refusals say why; started ones say what.
    const result = text("result");
    const what = text("repository") ?? text("title") ?? text("deploy") ?? text("incident");
    if (result && result !== "started") return `${verb}, refused: ${result}`;
    return what ? `${verb}: ${what}` : verb;
  }
  return verb;
}

/**
 * For the run, incident, deploy, and approvals views: why the signed-in person cannot approve
 * a given action in `workspaceId` (the run's workspace, which may not be the selected one).
 */
export function useApproveGate(workspaceId: string | undefined): (approval: Approval) => string | null {
  const { workspaces } = useWorkspace();
  const detail = useWorkspaceDetail(workspaceId);
  const role = detail.data?.role ?? workspaces.find((w) => w.id === workspaceId)?.role;
  const approvers = detail.data?.settings.emergency_approvers;
  return useCallback(
    (approval: Approval) => approveBlockedReason(approval.policy, role, approvers),
    [role, approvers],
  );
}

// ---------------------------------------------------------------------------- mutations

function withWorkspace(me: Me | undefined, workspace: Workspace): Me | undefined {
  if (!me) return me;
  const known = me.workspaces.some((w) => w.id === workspace.id);
  return {
    ...me,
    workspaces: known
      ? me.workspaces.map((w) => (w.id === workspace.id ? workspace : w))
      : [...me.workspaces, workspace],
  };
}

/** A new workspace owned by the signed-in user. It is added to /me at once so the caller can
 * switch to it before /me is fetched again. */
export function useCreateWorkspace() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (name: string) => unwrap(await api.POST("/workspaces", { body: { name } })),
    onSuccess: (created) => {
      client.setQueryData<Me>(meKey, (me) => withWorkspace(me, created));
      void client.invalidateQueries({ queryKey: meKey });
    },
  });
}

/** Rename and/or change settings (owners). `settings` replaces the whole object (left out,
 * `notifications` keeps the saved ones). `slack_webhook_url` saves the Slack webhook and null
 * removes it; the API never returns it, only `slack_saved`. */
export function useUpdateWorkspace(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (body: { name?: string; settings?: WorkspaceSettings; slack_webhook_url?: string | null }) =>
      unwrap(
        await api.PATCH("/workspaces/{workspace_id}", {
          params: { path: { workspace_id: workspaceId } },
          body,
        }),
      ),
    onSuccess: (detail) => {
      client.setQueryData(workspaceAdminKeys.detail(workspaceId), detail);
      client.setQueryData<Me>(meKey, (me) =>
        withWorkspace(me, { id: detail.id, name: detail.name, role: detail.role }),
      );
      // The daily spend cap shows in the summary; incident watch in the overview's checklist.
      void client.invalidateQueries({ queryKey: ["summary", workspaceId] });
      void client.invalidateQueries({ queryKey: ["workspace-config", workspaceId] });
    },
  });
}

/** Send a test notification to every channel set up (owners): one result per channel. */
export function useTestNotifications(workspaceId: string) {
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/workspaces/{workspace_id}/notifications/test", {
          params: { path: { workspace_id: workspaceId } },
        }),
      ),
  });
}

function useSetMembers(workspaceId: string) {
  const client = useQueryClient();
  return (members: Members) => client.setQueryData(workspaceAdminKeys.members(workspaceId), members);
}

/** Add by email or GitHub login (owners): someone with an account joins at once, anyone else
 * gets an invite (emailed, for an address). */
export function useAddMember(workspaceId: string) {
  const setMembers = useSetMembers(workspaceId);
  return useMutation({
    mutationFn: async (body: Invitee & { role: WorkspaceRole }) =>
      unwrap(
        await api.POST("/workspaces/{workspace_id}/members", {
          params: { path: { workspace_id: workspaceId } },
          body,
        }),
      ),
    onSuccess: setMembers,
  });
}

/** Change a member's role (owners). Changing your own role also changes what you can do. */
export function useUpdateMemberRole(workspaceId: string) {
  const client = useQueryClient();
  const setMembers = useSetMembers(workspaceId);
  return useMutation({
    mutationFn: async ({ userId, role }: { userId: string; role: WorkspaceRole }) =>
      unwrap(
        await api.PATCH("/workspaces/{workspace_id}/members/{user_id}", {
          params: { path: { workspace_id: workspaceId, user_id: userId } },
          body: { role },
        }),
      ),
    onSuccess: (members, { userId }) => {
      setMembers(members);
      const me = client.getQueryData<Me>(meKey);
      if (me?.user.id === userId) {
        void client.invalidateQueries({ queryKey: meKey });
        void client.invalidateQueries({ queryKey: workspaceAdminKeys.detail(workspaceId) });
      }
    },
  });
}

/** Remove someone else (owners). Leaving yourself is `useLeaveWorkspace`. */
export function useRemoveMember(workspaceId: string) {
  const setMembers = useSetMembers(workspaceId);
  return useMutation({
    mutationFn: async (userId: string) =>
      unwrap(
        await api.DELETE("/workspaces/{workspace_id}/members/{user_id}", {
          params: { path: { workspace_id: workspaceId, user_id: userId } },
        }),
      ),
    onSuccess: setMembers,
  });
}

export function useCancelInvite(workspaceId: string) {
  const setMembers = useSetMembers(workspaceId);
  return useMutation({
    mutationFn: async (inviteId: string) =>
      unwrap(
        await api.DELETE("/workspaces/{workspace_id}/invites/{invite_id}", {
          params: { path: { workspace_id: workspaceId, invite_id: inviteId } },
        }),
      ),
    onSuccess: setMembers,
  });
}

/** Leave a workspace (anyone but its last owner). It is dropped from /me at once. */
export function useLeaveWorkspace() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ workspaceId, userId }: { workspaceId: string; userId: string }) =>
      unwrap(
        await api.DELETE("/workspaces/{workspace_id}/members/{user_id}", {
          params: { path: { workspace_id: workspaceId, user_id: userId } },
        }),
      ),
    onSuccess: (_members, { workspaceId }) => {
      client.setQueryData<Me>(meKey, (me) =>
        me ? { ...me, workspaces: me.workspaces.filter((w) => w.id !== workspaceId) } : me,
      );
      client.removeQueries({ queryKey: workspaceAdminKeys.detail(workspaceId) });
      client.removeQueries({ queryKey: workspaceAdminKeys.members(workspaceId) });
      void client.invalidateQueries({ queryKey: meKey });
    },
  });
}

// ---------------------------------------------------------------------------- forms

const GITHUB_LOGIN = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$/;
const EMAIL = /^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$/;

/** "@Octocat " -> "Octocat"; null when it cannot be a GitHub login (the API checks the same). */
export function parseGitHubLogin(text: string): string | null {
  const login = text.trim().replace(/^@/, "");
  return GITHUB_LOGIN.test(login) ? login : null;
}

export type Invitee = { email: string } | { github_login: string };

/** Who to add: an email address (lowercased) or a GitHub login; null when it is neither. */
export function parseInvitee(text: string): Invitee | null {
  const value = text.trim();
  if (EMAIL.test(value)) return { email: value.toLowerCase() };
  const login = parseGitHubLogin(value);
  return login ? { github_login: login } : null;
}

export function ownerCount(members: Member[]): number {
  return members.filter((m) => m.role === "owner").length;
}

/** The settings form: the cap is text while it is edited. */
export type SettingsDraft = {
  emergencyApprovers: WorkspaceSettings["emergency_approvers"];
  incidentWatch: boolean;
  capMode: "default" | "custom";
  cap: string;
};

export const MAX_DAILY_CAP_USD = 10_000;

export function draftFromSettings(settings: WorkspaceSettings): SettingsDraft {
  const cap = settings.daily_usd_cap ?? null;
  return {
    emergencyApprovers: settings.emergency_approvers,
    incidentWatch: settings.incident_watch,
    capMode: cap === null ? "default" : "custom",
    cap: cap === null ? "" : String(cap),
  };
}

/** The settings to save (every field: the API replaces the whole object), or why not. */
export function settingsFromDraft(
  draft: SettingsDraft,
): { settings: WorkspaceSettings; error?: undefined } | { settings?: undefined; error: string } {
  let cap: number | null = null;
  if (draft.capMode === "custom") {
    const text = draft.cap.trim().replace(/^\$/, "");
    const value = Number(text);
    if (!text) return { error: "Enter a daily cap in USD, or use the platform default." };
    if (!Number.isFinite(value) || value < 0) return { error: "Enter an amount in USD, 0 or more." };
    if (value > MAX_DAILY_CAP_USD) return { error: "The cap can be at most $10,000 a day." };
    cap = Math.round(value * 100) / 100;
  }
  return {
    settings: {
      emergency_approvers: draft.emergencyApprovers,
      incident_watch: draft.incidentWatch,
      daily_usd_cap: cap,
    },
  };
}

export function sameSettings(a: WorkspaceSettings, b: WorkspaceSettings): boolean {
  return (
    a.emergency_approvers === b.emergency_approvers &&
    a.incident_watch === b.incident_watch &&
    (a.daily_usd_cap ?? null) === (b.daily_usd_cap ?? null)
  );
}

// ---------------------------------------------------------------------------- notifications

export type NotificationEvent = "incident_opened" | "approval_needed" | "incident_resolved" | "incident_failed";

export const NOTIFICATION_EVENTS: { key: NotificationEvent; label: string; hint: string }[] = [
  {
    key: "incident_opened",
    label: "Incident opened",
    hint: "Not for signals that join an open incident, and never for test incidents.",
  },
  { key: "approval_needed", label: "Approval needed", hint: "Medic proposed fixes and waits for an approval." },
  { key: "incident_resolved", label: "Incident resolved", hint: "The signals recovered." },
  { key: "incident_failed", label: "Incident failed", hint: "Medic could not fix it, or someone stopped it." },
];

export const DEFAULT_NOTIFICATIONS: NotificationSettings = {
  email: "owners",
  incident_opened: true,
  approval_needed: true,
  incident_resolved: true,
  incident_failed: true,
};

/** The saved notification settings, with the API's defaults for anything missing. */
export function notificationsOf(settings: WorkspaceSettings): NotificationSettings {
  return { ...DEFAULT_NOTIFICATIONS, ...settings.notifications };
}

export function sameNotifications(a: NotificationSettings, b: NotificationSettings): boolean {
  return a.email === b.email && NOTIFICATION_EVENTS.every(({ key }) => a[key] === b[key]);
}

const SLACK_WEBHOOK = /^https:\/\/hooks\.slack\.com\/services\/[A-Za-z0-9_-]+(?:\/[A-Za-z0-9_-]+)+$/;

/** Why `text` is not a Slack incoming webhook, or null when it is (the API checks the same). */
export function slackWebhookError(text: string): string | null {
  const value = text.trim();
  if (!value) return "Paste the webhook URL from Slack.";
  return SLACK_WEBHOOK.test(value)
    ? null
    : "That is not a Slack incoming webhook (https://hooks.slack.com/services/...).";
}
