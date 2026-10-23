"use client";

import { cn } from "cn";
import { Plus, SlidersHorizontal, Trash2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { type FormEvent, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { type DeploySettingsChange, useChangeSettings, useDeploySettings } from "@/lib/deploys";

/**
 * Change a live deploy's env vars without a rebuild. Groundcrew never stores values, so each
 * var the deploy has is kept as it is, given a new value, or removed; new ones can be added.
 * The change is a deploy of the running image that waits for approval like any other.
 */

type Choice = "keep" | "set" | "remove";
type Existing = { name: string; secret: boolean; choice: Choice; value: string };
type Added = { key: number; name: string; value: string; secret: boolean };

const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;
const SECRET_NAME = /(password|passwd|secret|token|api_?key|private_?key|credential|database_url|dsn)/i;

/** The request for these edits, or the problem that keeps it from being sent. */
export function settingsChange(
  existing: Existing[],
  added: Added[],
): { body: DeploySettingsChange } | { problem: string } {
  const env: DeploySettingsChange["env"] = [];
  const remove: string[] = [];
  const seen = new Set(existing.map((e) => e.name));
  for (const e of existing) {
    if (e.choice === "remove") remove.push(e.name);
    if (e.choice === "set") {
      if (!e.value) return { problem: `Give ${e.name} its new value, or keep it.` };
      env.push({ name: e.name, value: e.value, secret: e.secret });
    }
  }
  for (const a of added) {
    const name = a.name.trim();
    if (!name && !a.value) continue;
    if (!ENV_NAME.test(name)) return { problem: `${name || "A new variable"} needs a name of letters, digits, and _.` };
    if (seen.has(name)) return { problem: `${name} is listed twice.` };
    if (!a.value) return { problem: `Give ${name} a value.` };
    seen.add(name);
    env.push({ name, value: a.value, secret: a.secret });
  }
  if (env.length === 0 && remove.length === 0) return { problem: "Nothing is changed yet." };
  return { body: { env, remove } };
}

export function ChangeSettingsButton({ deployId }: { deployId: string }) {
  const [open, setOpen] = useState(false);
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm" variant="outline">
          <SlidersHorizontal aria-hidden="true" />
          Change settings
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-2xl">
        {open && <SettingsForm deployId={deployId} onDone={() => setOpen(false)} />}
      </DialogContent>
    </Dialog>
  );
}

function SettingsForm({ deployId, onDone }: { deployId: string; onDone: () => void }) {
  const id = useId();
  const router = useRouter();
  const settings = useDeploySettings(deployId, true);
  const change = useChangeSettings(deployId);
  const [edits, setEdits] = useState<Record<string, { choice: Choice; value: string }>>({});
  const [added, setAdded] = useState<Added[]>([]);
  const [nextKey, setNextKey] = useState(1);
  const [problem, setProblem] = useState<string | null>(null);

  const existing: Existing[] = (settings.data?.env ?? []).map((v) => ({
    ...v,
    choice: edits[v.name]?.choice ?? "keep",
    value: edits[v.name]?.value ?? "",
  }));

  function edit(name: string, patch: Partial<{ choice: Choice; value: string }>) {
    setEdits((current) => ({ ...current, [name]: { ...(current[name] ?? { choice: "keep", value: "" }), ...patch } }));
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    const result = settingsChange(existing, added);
    if ("problem" in result) {
      setProblem(result.problem);
      return;
    }
    setProblem(null);
    change.mutate(result.body, {
      onSuccess: (created) => {
        onDone();
        router.push(`/deploy/${created.deploy_id}`);
      },
    });
  }

  return (
    <form onSubmit={submit} className="space-y-5">
      <DialogHeader>
        <DialogTitle>Change settings</DialogTitle>
        <DialogDescription>
          Nothing is rebuilt: the image running now runs again with these env values, after you approve the plan. Values
          are never stored, so only the ones you set are sent; everything you keep stays as it is.
        </DialogDescription>
      </DialogHeader>

      {settings.isLoading && <p className="text-[13px] text-muted-foreground">Loading the current settings…</p>}
      {settings.data?.blocked && (
        <p role="alert" className="text-[13px] text-[var(--status-failed)]">
          {settings.data.blocked}
        </p>
      )}

      {settings.data && !settings.data.blocked && (
        <>
          <fieldset className="space-y-2">
            <legend className="mb-1 text-[13px] font-medium">Env vars on this deploy</legend>
            {existing.length === 0 && <p className="text-[12.5px] text-muted-foreground">None yet.</p>}
            <ul className="space-y-2">
              {existing.map((v) => (
                <li
                  key={v.name}
                  className="grid grid-cols-[minmax(0,1fr)_9rem] items-start gap-2 rounded-lg border p-2 sm:grid-cols-[minmax(0,1fr)_9rem_minmax(0,1.3fr)]"
                >
                  <span className="min-w-0 pt-1.5">
                    <span className={cn("font-mono text-[13px] break-all", v.choice === "remove" && "line-through")}>
                      {v.name}
                    </span>
                    {v.secret && <span className="ml-2 text-[11.5px] text-muted-foreground">secret</span>}
                  </span>
                  <Select value={v.choice} onValueChange={(c) => edit(v.name, { choice: c as Choice })}>
                    <SelectTrigger aria-label={`What to do with ${v.name}`} className="w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="keep">Keep</SelectItem>
                      <SelectItem value="set">New value</SelectItem>
                      <SelectItem value="remove">Remove</SelectItem>
                    </SelectContent>
                  </Select>
                  {v.choice === "set" ? (
                    <Input
                      aria-label={`New value of ${v.name}`}
                      value={v.value}
                      type={v.secret ? "password" : "text"}
                      autoComplete="off"
                      className="col-span-2 font-mono sm:col-span-1"
                      onChange={(e) => edit(v.name, { value: e.target.value })}
                    />
                  ) : (
                    <span className="col-span-2 pt-1.5 text-[12.5px] text-muted-foreground sm:col-span-1">
                      {v.choice === "remove" ? "Taken off the app" : "Stays as it is"}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </fieldset>

          <fieldset className="space-y-2">
            <legend className="mb-1 text-[13px] font-medium">Add</legend>
            {added.length > 0 && (
              <ul className="space-y-2">
                {added.map((a) => (
                  <li
                    key={a.key}
                    className="grid grid-cols-[1fr_auto] items-start gap-2 rounded-lg border p-2 sm:grid-cols-[1fr_1.3fr_auto_auto] sm:rounded-none sm:border-0 sm:p-0"
                  >
                    <div className="col-span-2 sm:col-span-1">
                      <Label htmlFor={`${id}-n${a.key}`} className="sr-only">
                        Name
                      </Label>
                      <Input
                        id={`${id}-n${a.key}`}
                        placeholder="NAME"
                        value={a.name}
                        autoComplete="off"
                        className="font-mono"
                        onChange={(e) =>
                          setAdded((list) =>
                            list.map((x) =>
                              x.key === a.key
                                ? { ...x, name: e.target.value, secret: SECRET_NAME.test(e.target.value) || x.secret }
                                : x,
                            ),
                          )
                        }
                      />
                    </div>
                    <div className="col-span-2 sm:col-span-1">
                      <Label htmlFor={`${id}-v${a.key}`} className="sr-only">
                        Value of {a.name || "the new variable"}
                      </Label>
                      <Input
                        id={`${id}-v${a.key}`}
                        placeholder="value"
                        value={a.value}
                        autoComplete="off"
                        type={a.secret ? "password" : "text"}
                        className="font-mono"
                        onChange={(e) =>
                          setAdded((list) => list.map((x) => (x.key === a.key ? { ...x, value: e.target.value } : x)))
                        }
                      />
                    </div>
                    <label className="flex h-9 items-center gap-1.5 text-[12.5px]">
                      <Switch
                        checked={a.secret}
                        aria-label={`${a.name || "New variable"} is a secret`}
                        onCheckedChange={(s) =>
                          setAdded((list) => list.map((x) => (x.key === a.key ? { ...x, secret: s } : x)))
                        }
                      />
                      Secret
                    </label>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-sm"
                      className="mt-1"
                      aria-label={`Remove ${a.name || "this new variable"}`}
                      onClick={() => setAdded((list) => list.filter((x) => x.key !== a.key))}
                    >
                      <Trash2 />
                    </Button>
                  </li>
                ))}
              </ul>
            )}
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => {
                setAdded((list) => [...list, { key: nextKey, name: "", value: "", secret: false }]);
                setNextKey((k) => k + 1);
              }}
            >
              <Plus aria-hidden="true" />
              Add env var
            </Button>
          </fieldset>
        </>
      )}

      {(problem || change.error) && (
        <p role="alert" className="text-[12.5px] text-[var(--status-failed)]">
          {problem ?? change.error?.message}
        </p>
      )}
      <DialogFooter>
        <Button type="button" variant="ghost" onClick={onDone}>
          Cancel
        </Button>
        <Button type="submit" disabled={!settings.data || Boolean(settings.data.blocked) || change.isPending}>
          {change.isPending ? "Starting" : "Review the plan"}
        </Button>
      </DialogFooter>
    </form>
  );
}
