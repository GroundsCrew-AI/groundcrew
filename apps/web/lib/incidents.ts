"use client";

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type Schemas, type Step, unwrap } from "@/lib/api/client";

export type IncidentSummary = Schemas["IncidentSummary"];
export type IncidentDetail = Schemas["IncidentDetail"];
export type IncidentCreate = Schemas["IncidentCreate"];
export type IncidentStatus = IncidentSummary["status"];
export type Severity = IncidentSummary["severity"];
export type DetectorRule = IncidentSummary["detector_rule"];
export type EvidenceItem = Schemas["EvidenceItem"];
export type ChangeMarker = Schemas["ChangeMarker"];
export type RootCause = Schemas["RootCause"];
export type ProposedFix = Schemas["ProposedFix"];
export type FixState = Schemas["FixState"];
export type FixApproval = Schemas["FixApproval"];

/**
 * Incident events on the Medic run's stream (docs/events.md): steps with typed `data`, sent
 * under their own names. `verify_result` is shared with deploys.
 */
export const MEDIC_EVENTS = [
  "incident_opened",
  "incident_updated",
  "evidence_collected",
  "memory_checked",
  "research_done",
  "diagnosis",
  "fix_proposed",
  "fix_applied",
  "incident_resolved",
  "incident_failed",
] as const;

export type MedicEventType = (typeof MEDIC_EVENTS)[number] | "fix_approval";

export function isMedicStep(step: Step): boolean {
  const type = step.data?.type;
  return Boolean(type && (MEDIC_EVENTS as readonly string[]).concat("fix_approval").includes(type));
}

export const OPEN_STATUSES: IncidentStatus[] = [
  "open",
  "investigating",
  "waiting_approval",
  "applying",
  "verifying",
];

export const STATUS: Record<IncidentStatus, { label: string; color: string }> = {
  open: { label: "Open", color: "var(--status-neutral)" },
  investigating: { label: "Investigating", color: "var(--status-running)" },
  waiting_approval: { label: "Needs approval", color: "var(--status-waiting)" },
  applying: { label: "Applying fix", color: "var(--status-running)" },
  verifying: { label: "Verifying", color: "var(--status-running)" },
  resolved: { label: "Resolved", color: "var(--status-ok)" },
  failed: { label: "Not resolved", color: "var(--status-failed)" },
};

export const SEVERITY: Record<Severity, { label: string; color: string }> = {
  critical: { label: "Critical", color: "var(--status-failed)" },
  high: { label: "High", color: "var(--status-waiting)" },
  medium: { label: "Medium", color: "var(--status-neutral)" },
  low: { label: "Low", color: "var(--subtle)" },
};

const RULES: Record<DetectorRule, string> = {
  health_check: "Health checks failing",
  ecs_deployment_failed: "Deployment failed",
  ecs_tasks_stopped: "Tasks crashing",
  log_error_spike: "Error spike in logs",
  rds_cpu_latency: "Database CPU and latency",
  user_report: "Reported by a person",
  other: "Other signal",
};

export function ruleLabel(rule: DetectorRule): string {
  return RULES[rule] ?? rule;
}

const ACTIONS: Record<string, string> = {
  ecs_rollback_service: "Roll back service",
  secret_rollback_version: "Roll back secret",
  force_redeploy: "Restart service",
  pull_request: "Fix pull request",
};

export function fixLabel(action: string): string {
  return ACTIONS[action] ?? action.replaceAll("_", " ");
}

export type IncidentFilters = { status?: IncidentStatus; severity?: Severity; resource?: string };

export const incidentKeys = {
  list: (ws: string, filters: IncidentFilters) => ["incidents", ws, filters] as const,
  detail: (id: string) => ["incident", id] as const,
};

export function useIncidents(workspaceId: string | undefined, filters: IncidentFilters, limit = 25) {
  return useInfiniteQuery({
    queryKey: [...incidentKeys.list(workspaceId ?? "", filters), limit],
    enabled: Boolean(workspaceId),
    initialPageParam: null as string | null,
    refetchInterval: 10_000,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await api.GET("/incidents", {
          params: {
            query: { workspace_id: workspaceId!, limit, cursor: pageParam ?? undefined, ...filters },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor,
  });
}

export function useIncident(incidentId: string) {
  return useQuery({
    queryKey: incidentKeys.detail(incidentId),
    // Open incidents change as Medic works; the page also refetches on stream events.
    refetchInterval: (query) =>
      query.state.data && OPEN_STATUSES.includes(query.state.data.status) ? 15_000 : false,
    queryFn: async (): Promise<IncidentDetail> =>
      unwrap(
        await api.GET("/incidents/{incident_id}", {
          params: { path: { incident_id: incidentId } },
        }),
      ),
  });
}

export function useReportIncident() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (body: IncidentCreate) => unwrap(await api.POST("/incidents", { body })),
    onSuccess: (_created, body) => {
      void client.invalidateQueries({ queryKey: ["incidents", body.workspace_id] });
    },
  });
}

/** Evidence items by id, so a claim can show the exact lines it cites. */
export function evidenceById(items: EvidenceItem[]): Map<string, EvidenceItem> {
  return new Map(items.map((item) => [item.id, item]));
}

/** "a/b" for a source key like "cloudwatch_logs:/ecs/checkout-api": reader and resource. */
export function sourceParts(source: string): { reader: string; resource: string } {
  const [reader, ...rest] = source.split(":");
  return { reader: reader ?? source, resource: rest.join(":") };
}
