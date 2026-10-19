"use client";

import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { api, type Approval, type Decision, unwrap } from "@/lib/api/client";

export const keys = {
  summary: (ws: string) => ["summary", ws] as const,
  runs: (ws: string) => ["runs", ws] as const,
  run: (id: string) => ["run", id] as const,
  approvals: (ws: string, tab: "pending" | "decided") => ["approvals", ws, tab] as const,
  config: (ws: string) => ["workspace-config", ws] as const,
};

/**
 * Counts for the overview and the sidebar badges. Every page has the sidebar, so this polls
 * gently; like every polling query it pauses while the tab is hidden (TanStack's default
 * refetchIntervalInBackground: false) and catches up on focus. Deciding an approval or starting
 * a run invalidates it straight away.
 */
export function useSummary(workspaceId: string | undefined) {
  return useQuery({
    queryKey: keys.summary(workspaceId ?? ""),
    enabled: Boolean(workspaceId),
    refetchInterval: 30_000,
    queryFn: async () =>
      unwrap(
        await api.GET("/workspaces/{workspace_id}/summary", {
          params: { path: { workspace_id: workspaceId! } },
        }),
      ),
  });
}

/**
 * The workspace's own settings (incident watch, emergency approvers, spend cap). Every member
 * can read them; only owners change them, in Settings.
 */
export function useWorkspaceConfig(workspaceId: string | undefined) {
  return useQuery({
    queryKey: keys.config(workspaceId ?? ""),
    enabled: Boolean(workspaceId),
    staleTime: 60_000,
    queryFn: async () =>
      unwrap(
        await api.GET("/workspaces/{workspace_id}", {
          params: { path: { workspace_id: workspaceId! } },
        }),
      ).settings,
  });
}

export function useRuns(workspaceId: string | undefined, limit = 25) {
  return useInfiniteQuery({
    queryKey: [...keys.runs(workspaceId ?? ""), limit],
    enabled: Boolean(workspaceId),
    initialPageParam: null as string | null,
    refetchInterval: 10_000,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await api.GET("/workspaces/{workspace_id}/runs", {
          params: {
            path: { workspace_id: workspaceId! },
            query: { limit, cursor: pageParam ?? undefined },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor,
  });
}

export function useRunDetail(runId: string, options: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: keys.run(runId),
    enabled: options.enabled ?? true,
    // The live stream keeps the page current; this is the starting point only.
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    queryFn: async () =>
      unwrap(await api.GET("/runs/{run_id}", { params: { path: { run_id: runId } } })),
  });
}

export function useApprovals(workspaceId: string | undefined, tab: "pending" | "decided") {
  return useQuery({
    queryKey: keys.approvals(workspaceId ?? "", tab),
    enabled: Boolean(workspaceId),
    refetchInterval: tab === "pending" ? 5_000 : 30_000,
    queryFn: async () =>
      unwrap(
        await api.GET("/workspaces/{workspace_id}/approvals", {
          params: {
            path: { workspace_id: workspaceId! },
            query: tab === "pending" ? { status: "pending" } : { decided: true },
          },
        }),
      ),
  });
}

export function useDecide() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ approval, decision }: { approval: Approval; decision: Decision }) =>
      unwrap(
        await api.POST("/approvals/{approval_id}/decide", {
          params: { path: { approval_id: approval.id } },
          body: { decision },
        }),
      ),
    onSettled: (_data, _error, { approval }) => {
      void client.invalidateQueries({ queryKey: ["approvals", approval.workspace_id] });
      void client.invalidateQueries({ queryKey: keys.summary(approval.workspace_id) });
    },
  });
}

export function useCreateRun() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (body: { workspace_id: string; agent: string; goal: string }) =>
      unwrap(await api.POST("/runs", { body })),
    onSuccess: (_data, body) => {
      void client.invalidateQueries({ queryKey: keys.runs(body.workspace_id) });
      void client.invalidateQueries({ queryKey: keys.summary(body.workspace_id) });
    },
  });
}

export function useStopRun() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (runId: string) =>
      unwrap(await api.POST("/runs/{run_id}/stop", { params: { path: { run_id: runId } } })),
    onSuccess: (run) => {
      void client.invalidateQueries({ queryKey: keys.runs(run.workspace_id) });
      void client.invalidateQueries({ queryKey: keys.summary(run.workspace_id) });
      void client.invalidateQueries({ queryKey: ["approvals", run.workspace_id] });
    },
  });
}
