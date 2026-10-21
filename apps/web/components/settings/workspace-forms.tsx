"use client";

import { type FormEvent, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { formatCost } from "@/lib/format";
import {
  draftFromSettings,
  MAX_DAILY_CAP_USD,
  sameSettings,
  type SettingsDraft,
  settingsFromDraft,
  useUpdateWorkspace,
  type WorkspaceDetail,
} from "@/lib/workspace-admin";

import { RadioOption } from "./settings-section";
import { toastResult } from "./toast-result";

/**
 * The workspace name. Owners rename it; members see it read-only. Mount it with a key of the
 * saved name so a rename from elsewhere resets the field.
 */
export function WorkspaceNameForm({ workspace, canEdit }: { workspace: WorkspaceDetail; canEdit: boolean }) {
  const update = useUpdateWorkspace(workspace.id);
  const [name, setName] = useState(workspace.name);
  const trimmed = name.trim();
  const dirty = trimmed !== workspace.name;

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!canEdit || !trimmed || !dirty) return;
    void toastResult(update.mutateAsync({ name: trimmed }), () => "Workspace renamed");
  }

  return (
    <form onSubmit={submit} className="space-y-2">
      <Label htmlFor="workspace-name">Name</Label>
      <div className="flex max-w-md gap-2">
        <Input
          id="workspace-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={200}
          disabled={!canEdit}
          required
        />
        {canEdit && (
          <Button type="submit" variant="outline" disabled={!trimmed || !dirty || update.isPending}>
            {update.isPending ? "Saving" : "Save"}
          </Button>
        )}
      </div>
    </form>
  );
}

/**
 * Who approves emergency actions, the incident watcher, and the daily model spend cap. The
 * API replaces the settings as a whole, so Save sends every field. Mount it with a key of the
 * saved settings so a change from elsewhere resets the draft.
 */
export function WorkspaceSettingsForm({
  workspace,
  canEdit,
  platformCap,
}: {
  workspace: WorkspaceDetail;
  canEdit: boolean;
  /** The platform's daily cap in USD, when known (shown next to "Platform default"). */
  platformCap: number | null;
}) {
  const id = useId();
  const update = useUpdateWorkspace(workspace.id);
  const [draft, setDraft] = useState<SettingsDraft>(() => draftFromSettings(workspace.settings));
  const [showError, setShowError] = useState(false);
  const parsed = settingsFromDraft(draft);
  const dirty = !parsed.settings || !sameSettings(parsed.settings, workspace.settings);
  const set = (patch: Partial<SettingsDraft>) => setDraft((d) => ({ ...d, ...patch }));
  const locked = !canEdit || update.isPending;

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!canEdit) return;
    if (!parsed.settings) {
      setShowError(true);
      return;
    }
    void toastResult(update.mutateAsync({ settings: parsed.settings }), () => "Settings saved");
  }

  const capError = showError && parsed.error ? parsed.error : null;
  return (
    <form onSubmit={submit} className="space-y-7" noValidate>
      <fieldset className="space-y-2">
        <legend className="mb-1 text-[13px] font-medium">Who can approve emergency actions</legend>
        <p className="mb-2 text-[12px] text-muted-foreground">
          Rollbacks, restarts, and other actions that change production at once. Owners and approvers can always
          reject them and approve pull requests and deploy plans; viewers never approve.
        </p>
        <RadioOption
          name={`${id}-approvers`}
          value="owners"
          checked={draft.emergencyApprovers === "owners"}
          disabled={locked}
          onChange={() => set({ emergencyApprovers: "owners" })}
          title="Owners only"
        />
        <RadioOption
          name={`${id}-approvers`}
          value="approvers"
          checked={draft.emergencyApprovers === "approvers"}
          disabled={locked}
          onChange={() => set({ emergencyApprovers: "approvers" })}
          title="Owners and approvers"
        />
      </fieldset>

      <div className="flex items-start justify-between gap-4">
        <div>
          <Label htmlFor={`${id}-watch`} className="text-[13px]">
            Incident watcher
          </Label>
          <p id={`${id}-watch-hint`} className="mt-1 text-[12px] text-muted-foreground">
            Opens an incident when a watched signal goes bad, so Medic can investigate. Off: only a person opens
            incidents. The watcher must also be on for this Groundcrew server.
          </p>
        </div>
        <Switch
          id={`${id}-watch`}
          checked={draft.incidentWatch}
          disabled={locked}
          onCheckedChange={(checked) => set({ incidentWatch: checked })}
          aria-describedby={`${id}-watch-hint`}
        />
      </div>

      <fieldset className="space-y-2">
        <legend className="mb-1 text-[13px] font-medium">Daily model spend</legend>
        <p className="mb-2 text-[12px] text-muted-foreground">
          Agents in this workspace stop calling models for the rest of the day (UTC) once it spends this much.
        </p>
        <RadioOption
          name={`${id}-cap`}
          value="default"
          checked={draft.capMode === "default"}
          disabled={locked}
          onChange={() => set({ capMode: "default" })}
          title={platformCap === null ? "Use the platform default" : `Use the platform default (${formatCost(platformCap)} a day)`}
        />
        <RadioOption
          name={`${id}-cap`}
          value="custom"
          checked={draft.capMode === "custom"}
          disabled={locked}
          onChange={() => set({ capMode: "custom" })}
          title="Set a lower cap for this workspace"
        />
        {draft.capMode === "custom" && (
          <div className="flex items-center gap-2 pt-1 pl-1 text-[13px]">
            <Label htmlFor={`${id}-cap-value`} className="sr-only">
              Daily cap in USD
            </Label>
            <span aria-hidden="true" className="text-muted-foreground">
              $
            </span>
            <Input
              id={`${id}-cap-value`}
              type="number"
              inputMode="decimal"
              min={0}
              max={MAX_DAILY_CAP_USD}
              step={0.01}
              value={draft.cap}
              onChange={(e) => set({ cap: e.target.value })}
              disabled={locked}
              aria-invalid={capError ? true : undefined}
              aria-describedby={capError ? `${id}-cap-error` : undefined}
              className="w-28 tabular"
            />
            <span className="text-muted-foreground">a day</span>
          </div>
        )}
        {capError && (
          <p id={`${id}-cap-error`} role="alert" className="text-[12px] text-[var(--status-failed)]">
            {capError}
          </p>
        )}
      </fieldset>

      {canEdit && (
        <div className="flex items-center gap-2">
          <Button type="submit" disabled={!dirty || update.isPending}>
            {update.isPending ? "Saving" : "Save settings"}
          </Button>
          {dirty && (
            <Button
              type="button"
              variant="ghost"
              disabled={update.isPending}
              onClick={() => {
                setDraft(draftFromSettings(workspace.settings));
                setShowError(false);
              }}
            >
              Discard changes
            </Button>
          )}
        </div>
      )}
    </form>
  );
}
