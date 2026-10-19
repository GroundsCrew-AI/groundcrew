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
export type FollowUp = Schemas["FollowUp"];
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
  // A newer incident on a related resource took it over: the outage goes on there.
  merged: { label: "Merged", color: "var(--status-neutral)" },
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

/** Opened within this long before now. */
export type IncidentWindow = "24h" | "7d" | "30d";

export type IncidentFilters = {
  status?: IncidentStatus;
  severity?: Severity;
  resource?: string;
  /** Only incidents whose permanent fix PR is not deployed yet. */
  pending_fix?: boolean;
  /**
   * Sent as `since`, worked out at fetch time: the query key keeps "7d" rather than a timestamp,
   * so polling keeps the same list and the window moves with the clock.
   */
  window?: IncidentWindow;
};

const WINDOW_MS: Record<IncidentWindow, number> = {
  "24h": 24 * 3_600_000,
  "7d": 7 * 24 * 3_600_000,
  "30d": 30 * 24 * 3_600_000,
};

export function isIncidentWindow(value: string | null | undefined): value is IncidentWindow {
  return Boolean(value && Object.hasOwn(WINDOW_MS, value));
}

/** The API's `since` for a time window, as of `now`. */
export function sinceFor(window: IncidentWindow | null | undefined, now: number): string | undefined {
  return window ? new Date(now - WINDOW_MS[window]).toISOString() : undefined;
}

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
    queryFn: async ({ pageParam }) => {
      const { window, ...query } = filters;
      const since = sinceFor(window, Date.now());
      return unwrap(
        await api.GET("/incidents", {
          params: {
            query: { workspace_id: workspaceId!, limit, cursor: pageParam ?? undefined, since, ...query },
          },
        }),
      );
    },
    getNextPageParam: (last) => last.next_cursor,
  });
}

export function useIncident(incidentId: string) {
  return useQuery({
    queryKey: incidentKeys.detail(incidentId),
    // Open incidents change as Medic works; the page also refetches on stream events.
    // A resolved incident with its fix PR pending changes as the PR is merged and deployed
    // (checked server-side every 2 minutes per PR).
    refetchInterval: (query) =>
      query.state.data && OPEN_STATUSES.includes(query.state.data.status)
        ? 15_000
        : query.state.data?.has_open_followups
          ? 60_000
          : false,
    queryFn: async (): Promise<IncidentDetail> =>
      unwrap(
        await api.GET("/incidents/{incident_id}", {
          params: { path: { incident_id: incidentId } },
        }),
      ),
  });
}

/**
 * Retry a failed emergency fix once its cause (e.g. a missing permission) is fixed: Medic plans
 * it again and asks for a new approval; nothing changes in AWS until someone approves.
 */
export function useRetryFix(incidentId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (fixId: string): Promise<IncidentDetail> =>
      unwrap(
        await api.POST("/incidents/{incident_id}/fixes/{fix_id}/retry", {
          params: { path: { incident_id: incidentId, fix_id: fixId } },
        }),
      ),
    onSuccess: (updated) => {
      client.setQueryData(incidentKeys.detail(incidentId), updated);
      void client.invalidateQueries({ queryKey: ["incidents"] });
    },
  });
}

/** A person says the change still in the repo is handled (change_in_repo follow-ups only). */
export function useDismissFollowUp(incidentId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (followupId: string): Promise<IncidentDetail> =>
      unwrap(
        await api.POST("/incidents/{incident_id}/followups/{followup_id}/dismiss", {
          params: { path: { incident_id: incidentId, followup_id: followupId } },
        }),
      ),
    onSuccess: (updated) => {
      client.setQueryData(incidentKeys.detail(incidentId), updated);
      void client.invalidateQueries({ queryKey: ["incidents"] });
    },
  });
}

/** A failed emergency fix that can be tried again (with a new approval). */
export function canRetryFix(incident: IncidentDetail, fix: IncidentDetail["fixes"][number]): boolean {
  return fix.fix.kind === "emergency" && fix.status === "failed" && incident.status !== "resolved";
}

export function useReportIncident() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (body: IncidentCreate) => unwrap(await api.POST("/incidents", { body })),
    onSuccess: (_created, body) => {
      void client.invalidateQueries({ queryKey: ["incidents", body.workspace_id] });
      // The sidebar badge and the overview count open incidents.
      void client.invalidateQueries({ queryKey: ["summary", body.workspace_id] });
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

type VerifyData = Extract<NonNullable<Step["data"]>, { type: "verify_result" }>;

/** Medic's recovery rounds carry a check number; a deploy's one verify does not. */
export function isMedicVerify(step: Step): boolean {
  return step.data?.type === "verify_result" && step.data.check !== null && step.data.check !== undefined;
}

/** What a round says, ignoring counts that change every round. */
function verifyKey(data: VerifyData): string {
  if (data.ok) return "ok";
  if (data.waiting) return `waiting:${data.waiting}`;
  const failing = data.checks
    .filter((c) => !c.ok)
    .map((c) => `${c.name}:${c.detail.split(":")[0]}`)
    .sort();
  return `failing:${[...new Set(failing)].join("|")}`;
}

/**
 * Medic verifies every 30 s; a row per round buried the timeline. Consecutive rounds that say
 * the same thing (waiting for the same rollout, the same checks still failing) show as their
 * latest round, with how many rounds it stands for.
 */
export function collapseVerifySteps(steps: Step[]): { hidden: Set<number>; rounds: Map<number, number> } {
  const hidden = new Set<number>();
  const rounds = new Map<number, number>();
  let run: Step[] = [];
  const flush = () => {
    if (run.length > 0) {
      for (const s of run.slice(0, -1)) hidden.add(s.seq);
      rounds.set(run[run.length - 1].seq, run.length);
    }
    run = [];
  };
  for (const step of steps) {
    if (!isMedicVerify(step)) {
      if (step.data?.type !== "incident_updated" || run.length === 0) flush();
      continue;
    }
    const previous = run[run.length - 1];
    if (previous && verifyKey(previous.data as VerifyData) !== verifyKey(step.data as VerifyData)) flush();
    run.push(step);
  }
  flush();
  return { hidden, rounds };
}

/** A round's check rows without repeats (the same check and detail listed twice). */
export function uniqueChecks(data: VerifyData): VerifyData["checks"] {
  const seen = new Set<string>();
  return data.checks.filter((c) => {
    const key = `${c.name}|${c.ok}|${c.detail}|${c.url ?? ""}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
