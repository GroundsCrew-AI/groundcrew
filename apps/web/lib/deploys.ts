"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type Schemas, type Step, unwrap } from "@/lib/api/client";

export type DeployCreate = Schemas["DeployCreate"];
export type DeployCreated = Schemas["DeployCreated"];
export type DeploySummary = Schemas["DeploySummary"];
export type DeployDetail = Schemas["DeployDetail"];
export type DeployPage = Schemas["DeployPage"];
export type DeployState = DeploySummary["status"];
export type DeployStage = Schemas["StageStarted"]["stage"];
export type TargetKind = Schemas["DeployPlan"]["target"];
export type EnvVarIn = Schemas["EnvVarIn"];
export type EnvVarView = Schemas["EnvVarView"];
export type StageStarted = Schemas["StageStarted"];
export type StageFinished = Schemas["StageFinished"];
export type BuildAttempt = Schemas["BuildAttempt"];
export type DeployPlan = Schemas["DeployPlan"];
export type VerifyResult = Schemas["VerifyResult"];
export type LiveUrl = Schemas["LiveUrl"];
export type RollbackOffered = Schemas["RollbackOffered"];
export type RunFinished = Schemas["RunFinished"];
export type DeployEventData = NonNullable<Step["data"]>;

/**
 * Fixing a failed env preflight from the approval card: set values, or continue without the
 * missing ones (POST /deploys/{id}/env). Only preflight runs again and the plan is re-rendered
 * with the same built image (no rebuild); the pending approval is replaced by a new one.
 */
export type EnvFix = Omit<Schemas["DeployEnvUpdate"], "approval_id">;

export const DEPLOY_EVENTS = [
  "stage_started",
  "stage_finished",
  "build_attempt",
  "verify_result",
  "live_url",
  "rollback_offered",
  "run_finished",
] as const;

/**
 * Stages in pipeline order; rollback only appears after a failed verify. There is no push
 * stage: nothing leaves the sandbox before approval, so the push is the first step of apply.
 */
export const STAGES: DeployStage[] = [
  "scout",
  "build",
  "smoke",
  "plan",
  "preflight",
  "approval",
  "apply",
  "verify",
];

export const deployKeys = {
  list: (ws: string) => ["deploys", ws] as const,
  detail: (id: string) => ["deploy", id] as const,
};

export function useDeploys(workspaceId: string | undefined) {
  return useQuery({
    queryKey: deployKeys.list(workspaceId ?? ""),
    enabled: Boolean(workspaceId),
    refetchInterval: 10_000,
    queryFn: async (): Promise<DeployPage> =>
      unwrap(await api.GET("/deploys", { params: { query: { workspace_id: workspaceId! } } })),
  });
}

export function useDeploy(deployId: string) {
  return useQuery({
    queryKey: deployKeys.detail(deployId),
    queryFn: async (): Promise<DeployDetail> =>
      unwrap(await api.GET("/deploys/{deploy_id}", { params: { path: { deploy_id: deployId } } })),
  });
}

export function useCreateDeploy() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (body: DeployCreate): Promise<DeployCreated> =>
      unwrap(await api.POST("/deploys", { body })),
    onSuccess: (_created, body) => {
      void client.invalidateQueries({ queryKey: deployKeys.list(body.workspace_id) });
    },
  });
}

export function useUpdateEnv(deployId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ approvalId, fix }: { approvalId: string; fix: EnvFix }) =>
      unwrap(
        await api.POST("/deploys/{deploy_id}/env", {
          params: { path: { deploy_id: deployId } },
          body: { approval_id: approvalId, ...fix },
        }),
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: deployKeys.detail(deployId) });
    },
  });
}

export function stageLabel(stage: DeployStage): string {
  return stage === "scout" ? "Scout" : stage.charAt(0).toUpperCase() + stage.slice(1);
}

export function targetLabel(kind: TargetKind): string {
  return kind === "k8s" ? "Kubernetes" : kind === "agent" ? "Server (agent)" : "AWS ECS";
}
