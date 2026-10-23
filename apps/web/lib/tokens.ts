"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type Schemas, unwrap } from "@/lib/api/client";

/** Workspace access tokens for assistants (Groundcrew's MCP server). Owners only. */
export type WorkspaceToken = Schemas["TokenOut"];
export type TokenCreated = Schemas["TokenCreated"];
export type TokenIn = Schemas["TokenIn"];

const tokensKey = (ws: string) => ["workspace-tokens", ws] as const;

export function useTokens(workspaceId: string | undefined, enabled: boolean) {
  return useQuery({
    queryKey: tokensKey(workspaceId ?? ""),
    enabled: Boolean(workspaceId) && enabled,
    queryFn: async (): Promise<WorkspaceToken[]> =>
      unwrap(await api.GET("/workspaces/{workspace_id}/tokens", { params: { path: { workspace_id: workspaceId! } } })),
  });
}

export function useCreateToken(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (body: TokenIn): Promise<TokenCreated> =>
      unwrap(
        await api.POST("/workspaces/{workspace_id}/tokens", { params: { path: { workspace_id: workspaceId } }, body }),
      ),
    onSuccess: () => void client.invalidateQueries({ queryKey: tokensKey(workspaceId) }),
  });
}

export function useRevokeToken(workspaceId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (tokenId: string): Promise<WorkspaceToken> =>
      unwrap(
        await api.DELETE("/workspaces/{workspace_id}/tokens/{token_id}", {
          params: { path: { workspace_id: workspaceId, token_id: tokenId } },
        }),
      ),
    onSuccess: () => void client.invalidateQueries({ queryKey: tokensKey(workspaceId) }),
  });
}

/** Where assistants connect: the API's /mcp. */
export function mcpUrl(apiUrl: string): string {
  return `${apiUrl.replace(/\/+$/, "")}/mcp`;
}

export function claudeCodeCommand(apiUrl: string, token: string): string {
  return `claude mcp add --transport http groundcrew ${mcpUrl(apiUrl)} --header "Authorization: Bearer ${token}"`;
}

export function cursorConfig(apiUrl: string, token: string): string {
  return JSON.stringify(
    { mcpServers: { groundcrew: { url: mcpUrl(apiUrl), headers: { Authorization: `Bearer ${token}` } } } },
    null,
    2,
  );
}
