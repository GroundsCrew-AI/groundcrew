"use client";

import { RefreshCw } from "lucide-react";
import Link from "next/link";
import { type CSSProperties, useState } from "react";

import { ProviderMark } from "@/components/map/resource-meta";
import { Button } from "@/components/ui/button";
import type { Connection } from "@/lib/discovery";
import { formatRelative } from "@/lib/format";

import { AwsOperateDialog, isOperate } from "./aws-operate-dialog";
import { AwsRegionsDialog } from "./aws-regions-dialog";
import { K8sConfigMapsDialog } from "./k8s-configmaps-dialog";
import { K8sDeploySettingsDialog } from "./k8s-deploy-settings-dialog";
import { K8sOperateDialog } from "./k8s-operate-dialog";
import { AwsTierDialog } from "./aws-tier-dialog";
import { RemoveConnection } from "./remove-connection";

const STATUS: Record<string, { label: string; color: string }> = {
  connected: { label: "Connected", color: "var(--status-ok)" },
  pending: { label: "Not finished", color: "var(--status-waiting)" },
  error: { label: "Error", color: "var(--status-failed)" },
  preview: { label: "Preview", color: "var(--status-neutral)" },
};

export function connectionStatus(connection: Connection): {
  label: string;
  color: string;
} {
  if (connection.preview) return STATUS.preview!;
  return STATUS[connection.status] ?? STATUS.error!;
}

function detail(connection: Connection): string {
  switch (connection.kind) {
    case "aws":
      return [
        connection.role_arn ?? `ExternalId ${connection.external_id ?? ""}`,
        (connection.regions ?? []).join(", "),
      ]
        .filter(Boolean)
        .join(" · ");
    case "github":
      return (connection.repos ?? []).join(", ") || "No repositories linked";
    case "vercel":
      return connection.team_id
        ? `Team ${connection.account ?? connection.team_id}`
        : "Personal account";
    case "azure":
      return `Resource group ${connection.resource_group ?? ""}`;
    default:
      return connection.server ?? "";
  }
}

export function lastDiscoveryText(connection: Connection): string {
  const last = connection.last_discovery;
  if (!last)
    return connection.status === "connected" && !connection.preview
      ? "Not discovered yet"
      : "";
  const when = formatRelative(last.at);
  if (last.status === "failed")
    return `Discovery failed ${when}: ${last.error ?? "unknown error"}`;
  const count = `${last.resources ?? connection.resource_count} resources`;
  return last.truncated
    ? `Discovered ${when}, ${count} (limit reached)`
    : `Discovered ${when}, ${count}`;
}

/** Why members see connection actions disabled; the API answers 403 for them too. */
export const OWNER_ONLY_REASON =
  "Only workspace owners can add, change, or remove connections.";

export function ConnectionRow({
  connection,
  onRefresh,
  refreshing,
  onResume,
  canManage = true,
  reasonId,
}: {
  connection: Connection;
  onRefresh: (connection: Connection) => void;
  refreshing: boolean;
  onResume?: (connection: Connection) => void;
  /** Owners finish, change, and remove connections; anyone in the workspace can refresh. */
  canManage?: boolean;
  /** The element that says why the owner-only actions are disabled. */
  reasonId?: string;
}) {
  const status = connectionStatus(connection);
  const last = connection.last_discovery;
  const [tierOpen, setTierOpen] = useState(false);
  const [operateOpen, setOperateOpen] = useState(false);
  const [regionsOpen, setRegionsOpen] = useState(false);
  const [deployOpen, setDeployOpen] = useState(false);
  const [configMapsOpen, setConfigMapsOpen] = useState(false);
  const awsConnected =
    connection.kind === "aws" && connection.status === "connected";
  const k8sConnected =
    connection.kind === "k8s" && connection.status === "connected";
  const ownerOnly = {
    disabled: !canManage,
    "aria-describedby": canManage ? undefined : reasonId,
  };
  return (
    <li className="flex flex-wrap items-center gap-3 bg-background px-4 py-3.5 sm:flex-nowrap">
      <ProviderMark provider={connection.kind} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="truncate font-medium">{connection.name}</h3>
          <span
            className="tint inline-flex h-5 items-center gap-1.5 rounded-full px-2 text-[12px] font-medium"
            style={{ "--tint": status.color } as CSSProperties}
          >
            <span
              aria-hidden="true"
              className="size-1.5 rounded-full bg-current"
            />
            {status.label}
          </span>
          {connection.kind === "aws" && connection.tier && (
            <span className="rounded-full border px-2 text-[12px] text-muted-foreground">
              {connection.tier === "deploy" ? "Deploy" : "Observe"}
            </span>
          )}
          {(connection.kind === "aws" || connection.kind === "k8s") &&
            isOperate(connection) && (
              <span className="rounded-full border px-2 text-[11.5px] text-muted-foreground">
                Emergency fixes
              </span>
            )}
          {connection.kind === "k8s" && connection.configmap_changes && (
            <span className="rounded-full border px-2 text-[11.5px] text-muted-foreground">
              ConfigMap changes
            </span>
          )}
          {connection.kind === "k8s" && connection.deploy_settings && (
            <span
              className="max-w-64 truncate rounded-full border px-2 text-[11.5px] text-muted-foreground"
              title={`Images go to ${connection.deploy_settings.registry}`}
            >
              Deploys to {connection.deploy_settings.registry}
            </span>
          )}
        </div>
        <p
          className="truncate font-mono text-[12px] text-muted-foreground"
          title={detail(connection)}
        >
          {detail(connection)}
        </p>
        <p
          className={
            last?.status === "failed"
              ? "text-[12px] text-[var(--status-failed)]"
              : "text-[12px] text-subtle"
          }
        >
          {lastDiscoveryText(connection)}
          {last?.run_id && (
            <>
              {" "}
              <Link
                href={`/runs/${last.run_id}`}
                className="underline-offset-2 hover:underline"
              >
                View run
              </Link>
            </>
          )}
        </p>
        {connection.error && connection.status !== "connected" && (
          <p className="text-[12px] text-[var(--status-failed)]">
            {connection.error}
          </p>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        {connection.status === "pending" &&
        connection.kind === "aws" &&
        onResume ? (
          <Button
            size="sm"
            variant="outline"
            onClick={() => onResume(connection)}
            {...ownerOnly}
          >
            Finish connecting
          </Button>
        ) : (
          <Button
            size="sm"
            variant="outline"
            disabled={
              connection.status !== "connected" ||
              connection.preview ||
              refreshing
            }
            onClick={() => onRefresh(connection)}
            title={
              connection.preview
                ? "Preview connections do not run discovery yet"
                : undefined
            }
          >
            <RefreshCw
              aria-hidden="true"
              className={refreshing ? "animate-spin" : undefined}
            />
            Refresh
          </Button>
        )}
        {awsConnected && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setRegionsOpen(true)}
            {...ownerOnly}
          >
            Regions
          </Button>
        )}
        {awsConnected && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setTierOpen(true)}
            {...ownerOnly}
          >
            {connection.tier === "deploy"
              ? "Turn off deploys"
              : "Enable deploys"}
          </Button>
        )}
        {k8sConnected && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setDeployOpen(true)}
            {...ownerOnly}
          >
            Deploy settings
          </Button>
        )}
        {k8sConnected && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setConfigMapsOpen(true)}
            {...ownerOnly}
          >
            {connection.configmap_changes
              ? "Stop ConfigMap detection"
              : "Detect ConfigMap changes"}
          </Button>
        )}
        {(awsConnected || k8sConnected) && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setOperateOpen(true)}
            {...ownerOnly}
          >
            {isOperate(connection)
              ? "Turn off emergency fixes"
              : "Enable emergency fixes"}
          </Button>
        )}
        <RemoveConnection
          connection={connection}
          disabled={!canManage}
          describedBy={ownerOnly["aria-describedby"]}
        />
      </div>
      {awsConnected && tierOpen && (
        <AwsTierDialog
          connection={connection}
          open
          onOpenChange={setTierOpen}
        />
      )}
      {awsConnected && regionsOpen && (
        <AwsRegionsDialog
          connection={connection}
          open
          onOpenChange={setRegionsOpen}
          onSaved={onRefresh}
        />
      )}
      {awsConnected && operateOpen && (
        <AwsOperateDialog
          connection={connection}
          open
          onOpenChange={setOperateOpen}
        />
      )}
      {k8sConnected && operateOpen && (
        <K8sOperateDialog
          connection={connection}
          open
          onOpenChange={setOperateOpen}
        />
      )}
      {k8sConnected && configMapsOpen && (
        <K8sConfigMapsDialog
          connection={connection}
          open
          onOpenChange={setConfigMapsOpen}
        />
      )}
      {k8sConnected && deployOpen && (
        <K8sDeploySettingsDialog
          connection={connection}
          open
          onOpenChange={setDeployOpen}
        />
      )}
    </li>
  );
}
