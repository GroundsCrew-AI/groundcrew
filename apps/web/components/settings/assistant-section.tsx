"use client";

import { Bot, KeyRound } from "lucide-react";
import Link from "next/link";
import { type FormEvent, useState } from "react";

import { CopyField } from "@/components/connections/copy-field";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { formatRelative, formatTimestamp } from "@/lib/format";
import {
  claudeCodeCommand,
  cursorConfig,
  type TokenCreated,
  useCreateToken,
  useRevokeToken,
  useTokens,
} from "@/lib/tokens";

import { toastResult } from "./toast-result";

const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/+$/, "");

const ROLES = {
  viewer: "Read: incidents, deploys, runs, approvals, and the Map",
  approver: "Read, and start work (deploys, settings changes, incident reports); never approve",
} as const;

/** Workspace tokens for assistants (Claude Code, Cursor, ...) through Groundcrew's MCP server. */
export function AssistantSection({ workspaceId, canManage }: { workspaceId: string; canManage: boolean }) {
  const tokens = useTokens(workspaceId, canManage);
  const create = useCreateToken(workspaceId);
  const revoke = useRevokeToken(workspaceId);
  const [name, setName] = useState("");
  const [role, setRole] = useState<"viewer" | "approver">("viewer");
  const [days, setDays] = useState("90");
  const [made, setMade] = useState<TokenCreated | null>(null);

  if (!canManage) {
    return <p className="text-[13px] text-muted-foreground">Owners make the tokens assistants use.</p>;
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate(
      { name: name.trim(), role, days: Number(days) },
      {
        onSuccess: (t) => {
          setMade(t);
          setName("");
        },
      },
    );
  }

  const live = (tokens.data ?? []).filter((t) => !t.revoked && new Date(t.expires_at) > new Date());

  return (
    <div className="space-y-5">
      <p className="text-[13px] text-muted-foreground">
        Ask your assistant about incidents and deploys from your editor. A token acts as you, in this workspace only.
        Anything that changes infrastructure still waits for an approval here.{" "}
        <Link href="/docs/assistants" className="text-foreground underline underline-offset-4">
          How it works
        </Link>
      </p>

      {made ? (
        <div className="space-y-3 rounded-lg border p-4">
          <p className="flex items-center gap-2 text-[13px] font-medium">
            <KeyRound aria-hidden="true" className="size-4" />
            {made.name}: copy it now, it is shown only once
          </p>
          <CopyField label="Token" value={made.token} />
          <CopyField label="Claude Code" value={claudeCodeCommand(API_URL, made.token)} />
          <CopyField label="Cursor (.cursor/mcp.json)" value={cursorConfig(API_URL, made.token)} multiline />
          <Button size="sm" variant="outline" onClick={() => setMade(null)}>
            Done
          </Button>
        </div>
      ) : (
        <form onSubmit={submit} className="grid gap-3 sm:grid-cols-[1.4fr_1fr_0.7fr_auto] sm:items-end">
          <div className="space-y-1.5">
            <Label htmlFor="token-name">Name</Label>
            <Input
              id="token-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Claude Code on my laptop"
              maxLength={100}
              required
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="token-role">Can</Label>
            <Select value={role} onValueChange={(v) => setRole(v as "viewer" | "approver")}>
              <SelectTrigger id="token-role" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="viewer">Read</SelectItem>
                <SelectItem value="approver">Read and start work</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="token-days">Expires in</Label>
            <Select value={days} onValueChange={setDays}>
              <SelectTrigger id="token-days" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="30">30 days</SelectItem>
                <SelectItem value="90">90 days</SelectItem>
                <SelectItem value="365">1 year</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <Button type="submit" disabled={create.isPending || !name.trim()}>
            <Bot aria-hidden="true" />
            {create.isPending ? "Creating" : "Create token"}
          </Button>
          <p className="text-[12px] text-muted-foreground sm:col-span-4">{ROLES[role]}.</p>
        </form>
      )}

      {live.length > 0 && (
        <ul className="divide-y rounded-lg border">
          {live.map((t) => (
            <li key={t.id} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-3 py-2.5 text-[13px]">
              <span className="min-w-0 flex-1">
                <span className="block font-medium">{t.name}</span>
                <span className="block text-[12px] text-muted-foreground">
                  <span className="font-mono">{t.prefix}…</span> · {t.role === "approver" ? "read and start work" : "read"} ·
                  expires {formatTimestamp(t.expires_at)} ·{" "}
                  {t.last_used_at ? `used ${formatRelative(t.last_used_at)}` : "not used yet"}
                </span>
              </span>
              <Button
                size="sm"
                variant="outline"
                disabled={revoke.isPending}
                onClick={() => void toastResult(revoke.mutateAsync(t.id), () => `Revoked ${t.name}`)}
              >
                Revoke
              </Button>
            </li>
          ))}
        </ul>
      )}
      {(create.error || revoke.error) && (
        <p role="alert" className="text-[12.5px] text-[var(--status-failed)]">
          {(create.error ?? revoke.error)?.message}
        </p>
      )}
    </div>
  );
}
