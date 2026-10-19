"use client";

import { CircleCheck, CircleX } from "lucide-react";
import { type FormEvent, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  type ChannelTestResult,
  NOTIFICATION_EVENTS,
  type NotificationSettings,
  notificationsOf,
  sameNotifications,
  slackWebhookError,
  useTestNotifications,
  useUpdateWorkspace,
  type WorkspaceDetail,
} from "@/lib/workspace-admin";

import { RadioOption } from "./settings-section";
import { toastResult } from "./toast-result";

/**
 * Where incident and approval notifications go: a Slack incoming webhook (write-only: the API
 * only says one is saved) and email to members, owners, or nobody; which events notify; and a
 * test send. Messages link to the incident page; approving stays in the app. Owners edit,
 * members see it read-only. Mount it with a key of the saved settings so a change from
 * elsewhere resets the draft.
 */
export function NotificationsSection({ workspace, canEdit }: { workspace: WorkspaceDetail; canEdit: boolean }) {
  const saved = notificationsOf(workspace.settings);
  return (
    <div className="space-y-7">
      <SlackWebhook workspace={workspace} canEdit={canEdit} />
      <NotificationsForm workspace={workspace} saved={saved} canEdit={canEdit} />
      {canEdit && <SendTest workspaceId={workspace.id} />}
    </div>
  );
}

function SlackWebhook({ workspace, canEdit }: { workspace: WorkspaceDetail; canEdit: boolean }) {
  const id = useId();
  const update = useUpdateWorkspace(workspace.id);
  const [url, setUrl] = useState("");
  const [editing, setEditing] = useState(false);
  const [showError, setShowError] = useState(false);
  const error = slackWebhookError(url);
  const showInput = canEdit && (!workspace.slack_saved || editing);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (error) {
      setShowError(true);
      return;
    }
    const done = await toastResult(update.mutateAsync({ slack_webhook_url: url.trim() }), () => "Slack webhook saved");
    if (done) {
      setUrl("");
      setEditing(false);
      setShowError(false);
    }
  }

  return (
    <div className="space-y-2">
      <Label htmlFor={`${id}-slack`} className="text-[13px]">
        Slack incoming webhook
      </Label>
      <p className="text-[12px] text-muted-foreground">
        Create one in Slack (Apps, Incoming Webhooks) for the channel to post to. It is stored encrypted and never
        shown again.
      </p>
      {workspace.slack_saved && !editing && (
        <div className="flex items-center gap-2 text-[13px]">
          <span className="inline-flex items-center gap-1.5">
            <CircleCheck aria-hidden="true" className="size-3.5 text-[var(--status-ok)]" />
            Saved
          </span>
          {canEdit && (
            <>
              <Button type="button" size="sm" variant="outline" onClick={() => setEditing(true)}>
                Replace
              </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                disabled={update.isPending}
                onClick={() =>
                  void toastResult(update.mutateAsync({ slack_webhook_url: null }), () => "Slack webhook removed")
                }
              >
                Clear
              </Button>
            </>
          )}
        </div>
      )}
      {!workspace.slack_saved && !canEdit && <p className="text-[13px] text-muted-foreground">Not set up.</p>}
      {showInput && (
        <form onSubmit={submit} className="space-y-1.5" noValidate>
          <div className="flex max-w-xl gap-2">
            <Input
              id={`${id}-slack`}
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder="https://hooks.slack.com/services/..."
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              disabled={update.isPending}
              aria-invalid={showError && error ? true : undefined}
              aria-describedby={showError && error ? `${id}-slack-error` : undefined}
            />
            <Button type="submit" variant="outline" disabled={!url.trim() || update.isPending}>
              {update.isPending ? "Saving" : "Save"}
            </Button>
            {editing && (
              <Button type="button" variant="ghost" onClick={() => (setEditing(false), setUrl(""))}>
                Cancel
              </Button>
            )}
          </div>
          {showError && error && (
            <p id={`${id}-slack-error`} role="alert" className="text-[12px] text-[var(--status-failed)]">
              {error}
            </p>
          )}
        </form>
      )}
    </div>
  );
}

function NotificationsForm({
  workspace,
  saved,
  canEdit,
}: {
  workspace: WorkspaceDetail;
  saved: NotificationSettings;
  canEdit: boolean;
}) {
  const id = useId();
  const update = useUpdateWorkspace(workspace.id);
  const [draft, setDraft] = useState<NotificationSettings>(saved);
  const dirty = !sameNotifications(draft, saved);
  const locked = !canEdit || update.isPending;
  const set = (patch: Partial<NotificationSettings>) => setDraft((d) => ({ ...d, ...patch }));

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!canEdit || !dirty) return;
    void toastResult(
      update.mutateAsync({ settings: { ...workspace.settings, notifications: draft } }),
      () => "Notifications saved",
    );
  }

  return (
    <form onSubmit={submit} className="space-y-7">
      <fieldset className="space-y-2">
        <legend className="mb-1 text-[13px] font-medium">Email</legend>
        <p className="mb-2 text-[12px] text-muted-foreground">
          Sent to confirmed addresses only. Needs SMTP on this Groundcrew server.
        </p>
        <RadioOption
          name={`${id}-email`}
          value="owners"
          checked={draft.email === "owners"}
          disabled={locked}
          onChange={() => set({ email: "owners" })}
          title="Owners only"
        />
        <RadioOption
          name={`${id}-email`}
          value="members"
          checked={draft.email === "members"}
          disabled={locked}
          onChange={() => set({ email: "members" })}
          title="Every member"
        />
        <RadioOption
          name={`${id}-email`}
          value="none"
          checked={draft.email === "none"}
          disabled={locked}
          onChange={() => set({ email: "none" })}
          title="No email"
        />
      </fieldset>

      <fieldset className="space-y-4">
        <legend className="mb-1 text-[13px] font-medium">Events</legend>
        {NOTIFICATION_EVENTS.map(({ key, label, hint }) => (
          <div key={key} className="flex items-start justify-between gap-4">
            <div>
              <Label htmlFor={`${id}-${key}`} className="text-[13px]">
                {label}
              </Label>
              <p id={`${id}-${key}-hint`} className="mt-1 text-[12px] text-muted-foreground">
                {hint}
              </p>
            </div>
            <Switch
              id={`${id}-${key}`}
              checked={draft[key]}
              disabled={locked}
              onCheckedChange={(checked) => set({ [key]: checked })}
              aria-describedby={`${id}-${key}-hint`}
            />
          </div>
        ))}
        <p className="text-[12px] text-muted-foreground">
          Messages link to the incident page. Approving and rejecting stay in Groundcrew: emails and Slack never carry
          approve links, since link scanners open every link they see.
        </p>
      </fieldset>

      {canEdit && (
        <div className="flex items-center gap-2">
          <Button type="submit" disabled={!dirty || update.isPending}>
            {update.isPending ? "Saving" : "Save notifications"}
          </Button>
          {dirty && (
            <Button type="button" variant="ghost" disabled={update.isPending} onClick={() => setDraft(saved)}>
              Discard changes
            </Button>
          )}
        </div>
      )}
    </form>
  );
}

const CHANNEL_LABEL: Record<ChannelTestResult["channel"], string> = { slack: "Slack", email: "Email" };

function SendTest({ workspaceId }: { workspaceId: string }) {
  const test = useTestNotifications(workspaceId);
  const results = test.data?.results;
  return (
    <div className="space-y-2 border-t pt-5">
      <div className="flex items-center gap-3">
        <Button type="button" variant="outline" disabled={test.isPending} onClick={() => test.mutate()}>
          {test.isPending ? "Sending" : "Send test"}
        </Button>
        <span className="text-[12px] text-muted-foreground">Sends a sample message to the saved channels now.</span>
      </div>
      {test.error && (
        <p role="alert" className="text-[12px] text-[var(--status-failed)]">
          {test.error.message}
        </p>
      )}
      {results && results.length === 0 && (
        <p role="status" className="text-[12.5px] text-muted-foreground">
          No channel is set up: save a Slack webhook or choose who gets email.
        </p>
      )}
      {results && results.length > 0 && (
        <ul role="status" className="space-y-1 text-[12.5px]">
          {results.map((r) => (
            <li key={r.channel} className="flex items-start gap-1.5">
              {r.ok ? (
                <CircleCheck aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-[var(--status-ok)]" />
              ) : (
                <CircleX aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-[var(--status-failed)]" />
              )}
              <span>
                <span className="font-medium">{CHANNEL_LABEL[r.channel]}:</span> {r.ok ? "" : "failed. "}
                {r.detail}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
