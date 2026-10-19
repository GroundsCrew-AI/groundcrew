"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type Schemas, unwrap } from "@/lib/api/client";

export type Connection = Schemas["ConnectionOut"];
export type ConnectionKind = Schemas["ConnectionKind"];
export type AwsStart = Schemas["AwsStartOut"];
export type GitHubInstallations = Schemas["GitHubInstallationsOut"];
export type ConnectionDeleted = Schemas["ConnectionDeleted"];
export type Graph = Schemas["GraphOut"];
export type GraphNode = Schemas["ResourceOut"];
export type GraphEdge = Schemas["EdgeOut"];
export type ResourceDetail = Schemas["ResourceDetail"];
export type ResourceEdge = Schemas["ResourceEdgeOut"];
export type SystemSummary = Schemas["SystemSummaryOut"];

export const discoveryKeys = {
  connections: (ws: string) => ["connections", ws] as const,
  installations: (ws: string) => ["github-installations", ws] as const,
  graph: (ws: string, includeStale: boolean) => ["graph", ws, includeStale] as const,
  resource: (id: string) => ["resource", id] as const,
};

export function useConnections(workspaceId: string | undefined) {
  return useQuery({
    queryKey: discoveryKeys.connections(workspaceId ?? ""),
    enabled: Boolean(workspaceId),
    refetchInterval: 10_000,
    queryFn: async () =>
      unwrap(await api.GET("/connections", { params: { query: { workspace_id: workspaceId! } } })),
  });
}

function useInvalidateWorkspace() {
  const client = useQueryClient();
  return (workspaceId: string) => {
    void client.invalidateQueries({ queryKey: discoveryKeys.connections(workspaceId) });
    void client.invalidateQueries({ queryKey: ["graph", workspaceId] });
    void client.invalidateQueries({ queryKey: ["runs", workspaceId] });
  };
}

export type AwsTier = Schemas["AwsTierIn"]["tier"];

export function useAwsStart() {
  return useMutation({
    mutationFn: async ({ workspaceId, tier }: { workspaceId: string; tier: AwsTier }) =>
      unwrap(await api.POST("/connections/aws/start", { body: { workspace_id: workspaceId, tier } })),
  });
}

/** Switch a connected AWS account's tier; deploy verifies the groundcrew-deploy role first. */
export function useAwsSetTier(workspaceId: string | undefined) {
  const invalidate = useInvalidateWorkspace();
  return useMutation({
    mutationFn: async ({ connectionId, tier }: { connectionId: string; tier: AwsTier }) =>
      unwrap(
        await api.POST("/connections/{connection_id}/aws/tier", {
          params: { path: { connection_id: connectionId } },
          body: { tier },
        }),
      ),
    onSettled: () => workspaceId && invalidate(workspaceId),
  });
}

/** Choose the regions Groundcrew reads and acts in for one AWS account ([] = the default). */
export function useAwsSetRegions(workspaceId: string | undefined) {
  const invalidate = useInvalidateWorkspace();
  return useMutation({
    mutationFn: async ({ connectionId, regions }: { connectionId: string; regions: string[] }) =>
      unwrap(
        await api.PUT("/connections/{connection_id}/aws/regions", {
          params: { path: { connection_id: connectionId } },
          body: { regions },
        }),
      ),
    onSettled: () => workspaceId && invalidate(workspaceId),
  });
}

/** Turn approved emergency fixes on (verifies the groundcrew-operate role first) or off. */
export function useAwsSetOperate(workspaceId: string | undefined) {
  const invalidate = useInvalidateWorkspace();
  return useMutation({
    mutationFn: async ({ connectionId, enabled }: { connectionId: string; enabled: boolean }) =>
      unwrap(
        await api.POST("/connections/{connection_id}/aws/operate", {
          params: { path: { connection_id: connectionId } },
          body: { enabled },
        }),
      ),
    onSettled: () => workspaceId && invalidate(workspaceId),
  });
}

/** Turn approved emergency fixes on for a cluster (checks the kubeconfig may patch Deployments)
 * or off. */
export function useK8sSetOperate(workspaceId: string | undefined) {
  const invalidate = useInvalidateWorkspace();
  return useMutation({
    mutationFn: async ({ connectionId, enabled }: { connectionId: string; enabled: boolean }) =>
      unwrap(
        await api.POST("/connections/{connection_id}/k8s/operate", {
          params: { path: { connection_id: connectionId } },
          body: { enabled },
        }),
      ),
    onSettled: () => workspaceId && invalidate(workspaceId),
  });
}

export function useAwsComplete(workspaceId: string | undefined) {
  const invalidate = useInvalidateWorkspace();
  return useMutation({
    mutationFn: async (body: { connection_id: string; role_arn: string }) =>
      unwrap(await api.POST("/connections/aws/complete", { body })),
    onSettled: () => workspaceId && invalidate(workspaceId),
  });
}

export function useGitHubInstallations(workspaceId: string | undefined, enabled: boolean) {
  return useQuery({
    queryKey: discoveryKeys.installations(workspaceId ?? ""),
    enabled: Boolean(workspaceId) && enabled,
    queryFn: async () =>
      unwrap(
        await api.GET("/connections/github/installations", {
          params: { query: { workspace_id: workspaceId! } },
        }),
      ),
  });
}

export function useLinkGitHub() {
  const invalidate = useInvalidateWorkspace();
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (body: { workspace_id: string; installation_id: number; repos: string[] }) =>
      unwrap(await api.POST("/connections/github", { body })),
    onSuccess: (_data, body) => {
      invalidate(body.workspace_id);
      void client.invalidateQueries({ queryKey: discoveryKeys.installations(body.workspace_id) });
    },
  });
}

export function useConnectVercel() {
  const invalidate = useInvalidateWorkspace();
  return useMutation({
    mutationFn: async (body: { workspace_id: string; token: string; team_id: string | null }) =>
      unwrap(await api.POST("/connections/vercel", { body })),
    onSuccess: (_data, body) => invalidate(body.workspace_id),
  });
}

export function useConnectAzure() {
  const invalidate = useInvalidateWorkspace();
  return useMutation({
    mutationFn: async (body: Schemas["AzureConnectIn"]) =>
      unwrap(await api.POST("/connections/azure", { body })),
    onSuccess: (_data, body) => invalidate(body.workspace_id),
  });
}

export function useConnectKubernetes() {
  const invalidate = useInvalidateWorkspace();
  return useMutation({
    mutationFn: async (body: Schemas["KubernetesConnectIn"]) =>
      unwrap(await api.POST("/connections/k8s", { body })),
    onSuccess: (_data, body) => invalidate(body.workspace_id),
  });
}

export function useDeleteConnection() {
  const invalidate = useInvalidateWorkspace();
  return useMutation({
    mutationFn: async (connection: Connection) =>
      unwrap(
        await api.DELETE("/connections/{connection_id}", {
          params: { path: { connection_id: connection.id } },
        }),
      ),
    onSuccess: (_data, connection) => invalidate(connection.workspace_id),
  });
}

export function useRefreshConnection() {
  const invalidate = useInvalidateWorkspace();
  return useMutation({
    mutationFn: async (connection: Connection) =>
      unwrap(
        await api.POST("/connections/{connection_id}/discover", {
          params: { path: { connection_id: connection.id } },
        }),
      ),
    onSuccess: (_data, connection) => invalidate(connection.workspace_id),
  });
}

export function useDiscoverWorkspace() {
  const invalidate = useInvalidateWorkspace();
  return useMutation({
    mutationFn: async (workspaceId: string) =>
      unwrap(
        await api.POST("/workspaces/{workspace_id}/discover", {
          params: { path: { workspace_id: workspaceId } },
        }),
      ),
    onSuccess: (_data, workspaceId) => invalidate(workspaceId),
  });
}

export function useGraph(workspaceId: string | undefined, includeStale: boolean) {
  return useQuery({
    queryKey: discoveryKeys.graph(workspaceId ?? "", includeStale),
    enabled: Boolean(workspaceId),
    // Fast while a discovery runs, so the map fills in as soon as it finishes.
    refetchInterval: (query) =>
      ["queued", "running"].includes(query.state.data?.last_run_status ?? "") ? 4_000 : 60_000,
    queryFn: async () =>
      unwrap(
        await api.GET("/workspaces/{workspace_id}/graph", {
          params: { path: { workspace_id: workspaceId! }, query: { include_stale: includeStale } },
        }),
      ),
  });
}

export function useResource(resourceId: string | null) {
  return useQuery({
    queryKey: discoveryKeys.resource(resourceId ?? ""),
    enabled: Boolean(resourceId),
    queryFn: async () =>
      unwrap(
        await api.GET("/resources/{resource_id}", {
          params: { path: { resource_id: resourceId! } },
        }),
      ),
  });
}

/** The CloudFormation template, downloaded through the proxy (it needs the session). */
/** The connect template's S3 URL (null when the server has none) for updating a stack. */
export function useAwsTemplateUrl(enabled = true) {
  return useQuery({
    queryKey: ["aws-template-url"],
    queryFn: async () => unwrap(await api.GET("/connections/aws/template-url")),
    enabled,
    staleTime: Infinity,
  });
}

export async function downloadAwsTemplate(): Promise<void> {
  const response = await fetch("/api/gc/connections/aws/template");
  if (!response.ok) throw new Error(`Could not download the template (${response.status}).`);
  const blob = new Blob([await response.text()], { type: "text/yaml" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "aws-connect.yaml";
  link.click();
  URL.revokeObjectURL(url);
}
