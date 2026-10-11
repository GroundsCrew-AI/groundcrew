"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError, type Schemas, type Step, unwrap } from "@/lib/api/client";
// TODO(M6): delete with lib/deploys-fake.ts once POST /deploys and GET /deploys answer.
import * as fake from "@/lib/deploys-fake";

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

/** SSE event names a deploy run adds (docs/events.md). Each carries a StepOut with `data`. */
export const DEPLOY_EVENTS = [
  "stage_started",
  "stage_finished",
  "build_attempt",
  "verify_result",
  "live_url",
  "rollback_offered",
  "run_finished",
] as const;

/** Stages in pipeline order; rollback only appears after a failed verify. */
export const STAGES: DeployStage[] = [
  "scout",
  "build",
  "smoke",
  "push",
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

function notReady(error: unknown): boolean {
  return error instanceof ApiError && error.status === 501;
}

export function useDeploys(workspaceId: string | undefined) {
  return useQuery({
    queryKey: deployKeys.list(workspaceId ?? ""),
    enabled: Boolean(workspaceId),
    refetchInterval: 10_000,
    queryFn: async (): Promise<DeployPage & { preview: boolean }> => {
      try {
        const page = unwrap(
          await api.GET("/deploys", { params: { query: { workspace_id: workspaceId! } } }),
        );
        return { ...page, preview: false };
      } catch (error) {
        if (notReady(error)) return { ...fake.listDeploys(workspaceId!), preview: true };
        throw error;
      }
    },
  });
}

export function useDeploy(deployId: string) {
  return useQuery({
    queryKey: deployKeys.detail(deployId),
    queryFn: async (): Promise<DeployDetail & { preview: boolean }> => {
      try {
        const detail = unwrap(
          await api.GET("/deploys/{deploy_id}", { params: { path: { deploy_id: deployId } } }),
        );
        return { ...detail, preview: false };
      } catch (error) {
        const preview = notReady(error) ? fake.getDeploy(deployId) : null;
        if (preview) return { ...preview, preview: true };
        if (notReady(error)) throw new ApiError("Deploy not found.", 404);
        throw error;
      }
    },
  });
}

export function useCreateDeploy() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (
      body: DeployCreate & { labels: fake.PreviewLabels },
    ): Promise<DeployCreated & { preview: boolean }> => {
      const { labels, ...request } = body;
      try {
        return { ...unwrap(await api.POST("/deploys", { body: request })), preview: false };
      } catch (error) {
        if (notReady(error)) return { ...fake.createDeploy(request, labels), preview: true };
        throw error;
      }
    },
    onSuccess: (_created, body) => {
      void client.invalidateQueries({ queryKey: deployKeys.list(body.workspace_id) });
    },
  });
}

export function stageLabel(stage: DeployStage): string {
  return stage === "scout" ? "Scout" : stage.charAt(0).toUpperCase() + stage.slice(1);
}

export function targetLabel(kind: TargetKind): string {
  return kind === "k8s" ? "Kubernetes" : "AWS ECS";
}
