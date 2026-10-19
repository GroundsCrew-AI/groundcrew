"use client";

import { BadgeCheck, CircleAlert } from "lucide-react";
import { type FormEvent, useState } from "react";

import { signOutAfterPasswordChange } from "@/app/actions";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { UserAvatar } from "@/components/user-avatar";
import { newPasswordProblem, SIGN_IN_METHOD_LABEL, useChangePassword, useUpdateProfile } from "@/lib/account";
import type { Me } from "@/lib/api/client";

import { SettingsSection } from "./settings-section";
import { toastResult } from "./toast-result";

const MIN_PASSWORD = 10;

function ProfileForm({ user }: { user: Me["user"] }) {
  const update = useUpdateProfile();
  const [name, setName] = useState(user.name ?? "");
  const changed = name.trim() !== (user.name ?? "") && name.trim().length > 0;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (changed) await toastResult(update.mutateAsync(name.trim()), () => "Name saved");
  }

  return (
    <form onSubmit={submit} className="space-y-4">
      <div className="flex items-center gap-3">
        <UserAvatar src={user.avatar_url} name={user.name || user.email || "You"} className="size-10" />
        <div className="min-w-0 text-[13px]">
          <p className="flex flex-wrap items-center gap-1.5">
            <span className="truncate">{user.email ?? "No email address"}</span>
            {user.email &&
              (user.email_verified ? (
                <span className="inline-flex items-center gap-1 text-[12px] text-[var(--status-succeeded)]">
                  <BadgeCheck aria-hidden="true" className="size-3.5" />
                  Confirmed
                </span>
              ) : (
                <span className="inline-flex items-center gap-1 text-[12px] text-[var(--status-waiting)]">
                  <CircleAlert aria-hidden="true" className="size-3.5" />
                  Not confirmed
                </span>
              ))}
          </p>
          {user.github_login && <p className="text-[12px] text-muted-foreground">@{user.github_login} on GitHub</p>}
        </div>
      </div>
      <div className="flex max-w-md items-end gap-2">
        <div className="flex-1 space-y-1.5">
          <Label htmlFor="profile-name" className="text-[12px] font-normal text-muted-foreground">
            Display name
          </Label>
          <Input id="profile-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
        </div>
        <Button type="submit" size="sm" variant="outline" className="h-9" disabled={!changed || update.isPending}>
          {update.isPending ? "Saving" : "Save"}
        </Button>
      </div>
    </form>
  );
}

function PasswordForm({ user }: { user: Me["user"] }) {
  const change = useChangePassword();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const adding = !user.has_password;

  async function submit(event: FormEvent) {
    event.preventDefault();
    const problem = newPasswordProblem(next, confirm, MIN_PASSWORD);
    if (problem) {
      setError(problem);
      return;
    }
    setError(null);
    try {
      await change.mutateAsync({ current_password: adding ? null : current, new_password: next });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not change the password. Try again.");
      return;
    }
    // Every session ended with the change, this one too.
    await signOutAfterPasswordChange();
  }

  if (adding && !user.email) {
    return (
      <p className="text-[12.5px] text-muted-foreground">
        Your account has no email address, so it signs in with {(user.sign_in_methods ?? []).map((m) => SIGN_IN_METHOD_LABEL[m] ?? m).join(" or ")} only.
      </p>
    );
  }

  return (
    <form onSubmit={submit} className="max-w-md space-y-3" noValidate>
      {adding && (
        <p className="text-[12.5px] text-muted-foreground">
          Add a password to also sign in with {user.email} and a password.
        </p>
      )}
      {!adding && (
        <div className="space-y-1.5">
          <Label htmlFor="current-password" className="text-[12px] font-normal text-muted-foreground">
            Current password
          </Label>
          <Input
            id="current-password"
            type="password"
            autoComplete="current-password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
        </div>
      )}
      <div className="space-y-1.5">
        <Label htmlFor="new-password" className="text-[12px] font-normal text-muted-foreground">
          New password
        </Label>
        <Input
          id="new-password"
          type="password"
          autoComplete="new-password"
          value={next}
          onChange={(e) => setNext(e.target.value)}
          aria-describedby="new-password-hint"
        />
        <p id="new-password-hint" className="text-[12px] text-muted-foreground">
          At least {MIN_PASSWORD} characters. Saving signs you out everywhere, here too.
        </p>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="confirm-password" className="text-[12px] font-normal text-muted-foreground">
          Confirm new password
        </Label>
        <Input
          id="confirm-password"
          type="password"
          autoComplete="new-password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
        />
      </div>
      {error && (
        <p role="alert" className="text-[12px] text-[var(--status-failed)]">
          {error}
        </p>
      )}
      <Button type="submit" size="sm" disabled={change.isPending || !next || (!adding && !current)}>
        {change.isPending ? "Saving" : adding ? "Add password" : "Change password"}
      </Button>
    </form>
  );
}

/** Your profile, how you sign in, and your password. */
export function AccountSection({ me }: { me: Me }) {
  const user = me.user;
  return (
    <div>
      <SettingsSection id="settings-profile" title="Your account" description="How you appear to your teammates.">
        <ProfileForm key={user.name ?? ""} user={user} />
      </SettingsSection>
      <SettingsSection
        id="settings-sign-in"
        title="Sign-in and password"
        description={
          <>
            You sign in with{" "}
            {(user.sign_in_methods ?? []).length
              ? (user.sign_in_methods ?? []).map((m) => SIGN_IN_METHOD_LABEL[m] ?? m).join(", ")
              : "a provider"}
            . Google and GitHub accounts with your confirmed address are linked automatically.
          </>
        }
      >
        <PasswordForm user={user} />
      </SettingsSection>
    </div>
  );
}
