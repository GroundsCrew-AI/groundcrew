"use client";

import { useQuery } from "@tanstack/react-query";
import { createContext, type ReactNode, useCallback, useContext, useMemo, useState } from "react";

import { api, type Me, unwrap, type Workspace } from "@/lib/api/client";

const STORAGE_KEY = "groundcrew.workspace";

type WorkspaceContextValue = {
  me: Me | undefined;
  workspace: Workspace | undefined;
  workspaces: Workspace[];
  setWorkspaceId: (id: string) => void;
  isLoading: boolean;
  error: Error | null;
  refetch: () => void;
};

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

function readStored(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function useMe() {
  return useQuery({
    queryKey: ["me"],
    queryFn: async () => unwrap(await api.GET("/me")),
    staleTime: 60_000,
  });
}

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const me = useMe();
  const [selected, setSelected] = useState<string | null>(() =>
    typeof window === "undefined" ? null : readStored(),
  );
  const workspaces = useMemo(() => me.data?.workspaces ?? [], [me.data]);
  const workspace = workspaces.find((w) => w.id === selected) ?? workspaces[0];

  const setWorkspaceId = useCallback((id: string) => {
    setSelected(id);
    try {
      window.localStorage.setItem(STORAGE_KEY, id);
    } catch {
      // Private mode or blocked storage: the choice just won't persist.
    }
  }, []);

  const value = useMemo<WorkspaceContextValue>(
    () => ({
      me: me.data,
      workspace,
      workspaces,
      setWorkspaceId,
      isLoading: me.isLoading,
      error: me.error,
      refetch: () => void me.refetch(),
    }),
    [me, workspace, workspaces, setWorkspaceId],
  );
  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

export function useWorkspace(): WorkspaceContextValue {
  const value = useContext(WorkspaceContext);
  if (!value) throw new Error("useWorkspace must be used inside WorkspaceProvider");
  return value;
}
