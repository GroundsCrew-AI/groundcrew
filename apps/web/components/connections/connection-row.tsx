"use client";

import { RefreshCw } from "lucide-react";
import Link from "next/link";
import { type CSSProperties, useState } from "react";

import { ProviderMark } from "@/components/map/resource-meta";
import { Button } from "@/components/ui/button";
import type { Connection } from "@/lib/discovery";
import { formatRelative } from "@/lib/format";

import { AwsOperateDialog, isOperate } from "./aws-operate-dialog";
import { AwsTierDialog } from "./aws-tier-dialog";
import { RemoveConnection } from "./remove-connection";

const STATUS: Record<string, { label: string; color: string }> = {
  connected: { label: "Connected", color: "var(--status-ok)" },
  pending: { label: "Not finished", color: "var(--status-waiting)" },
  error: { label: "Error", color: "var(--status-failed)" },
  preview: { label: "Preview", color: "var(--status-neutral)" },
};

export function connectionStatus(connection: Connection): { label: string; color: string } {
  if (connection.preview) return STATUS.preview!;
  return STATUS[connection.status] ?? STATUS.error!;
}

function detail(connection: Connection): string {
  switch (connection.kind) {
    case "aws":
      return connection.role_arn ?? `ExternalId ${connection.external_id ?? ""}`;
    case "github":
      return (connection.repos ?? []).join(", ") || "No repositories linked";
    case "vercel":
      return connection.team_id ? `Team ${connection.account ?? connection.team_id}` : "Personal account";
    case "azure":
      return `Resource group ${connection.resource_group ?? ""}`;
    default:
      return connection.server ?? "";
  }
}

export function lastDiscoveryText(connection: Connection): string {
  const last = connection.last_discovery;
  if (!last) return connection.status === "connected" && !connection.preview ? "Not discovered yet" : "";
  const when = formatRelative(last.at);
  if (last.status === "failed") return `Discovery failed ${when}: ${last.error ?? "unknown error"}`;
  const count = `${last.resources ?? connection.resource_count} resources`;
  return last.truncated ? `Discovered ${when}, ${count} (limit reached)` : `Discovered ${when}, ${count}`;
}

export function ConnectionRow({
  connection,
  onRefresh,
  refreshing,
  onResume,
}: {
  connection: Connection;
  onRefresh: (connection: Connection) => void;
  refreshing: boolean;
  onResume?: (connection: Connection) => void;
}) {
  const status = connectionStatus(connection);
  const last = connection.last_discovery;
  const [tierOpen, setTierOpen] = useState(false);
  const [operateOpen, setOperateOpen] = useState(false);
  const awsConnected = connection.kind === "aws" && connection.status === "connected";
  return (
    <li className="flex flex-wrap items-center gap-3 bg-background px-4 py-3.5 sm:flex-nowrap">
      <ProviderMark provider={connection.kind} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="truncate font-medium">{connection.name}</h3>
          <span
            className="tint inline-flex h-5 items-center gap-1.5 rounded-full px-2 text-[11.5px] font-medium"
            style={{ "--tint": status.color } as CSSProperties}
          >
            <span aria-hidden="true" className="size-1.5 rounded-full bg-current" />
            {status.label}
          </span>
          {connection.kind === "aws" && connection.tier && (
            <span className="rounded-full border px-2 text-[11.5px] text-muted-foreground">
              {connection.tier === "deploy" ? "Deploy" : "Observe"}
            </span>
          )}
          {connection.kind === "aws" && isOperate(connection) && (
            <span className="rounded-full border px-2 text-[11.5px] text-muted-foreground">Emergency fixes</span>
          )}
        </div>
        <p className="truncate font-mono text-[11.5px] text-muted-foreground" title={detail(connection)}>
          {detail(connection)}
        </p>
        <p className={last?.status === "failed" ? "text-[12px] text-[var(--status-failed)]" : "text-[12px] text-subtle"}>
          {lastDiscoveryText(connection)}
          {last?.run_id && (
            <>
              {" "}
              <Link href={`/runs/${last.run_id}`} className="underline-offset-2 hover:underline">
                View run
              </Link>
            </>
          )}
        </p>
        {connection.error && connection.status !== "connected" && (
          <p className="text-[12px] text-[var(--status-failed)]">{connection.error}</p>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        {connection.status === "pending" && connection.kind === "aws" && onResume ? (
          <Button size="sm" variant="outline" onClick={() => onResume(connection)}>
            Finish connecting
          </Button>
        ) : (
          <Button
            size="sm"
            variant="outline"
            disabled={connection.status !== "connected" || connection.preview || refreshing}
            onClick={() => onRefresh(connection)}
            title={connection.preview ? "Preview connections do not run discovery yet" : undefined}
          >
            <RefreshCw aria-hidden="true" className={refreshing ? "animate-spin" : undefined} />
            Refresh
          </Button>
        )}
        {awsConnected && (
          <Button size="sm" variant="ghost" onClick={() => setTierOpen(true)}>
            {connection.tier === "deploy" ? "Turn off deploys" : "Enable deploys"}
          </Button>
        )}
        {awsConnected && (
          <Button size="sm" variant="ghost" onClick={() => setOperateOpen(true)}>
            {isOperate(connection) ? "Turn off emergency fixes" : "Enable emergency fixes"}
          </Button>
        )}
        <RemoveConnection connection={connection} />
      </div>
      {awsConnected && tierOpen && <AwsTierDialog connection={connection} open onOpenChange={setTierOpen} />}
      {awsConnected && operateOpen && (
        <AwsOperateDialog connection={connection} open onOpenChange={setOperateOpen} />
      )}
    </li>
  );
}
