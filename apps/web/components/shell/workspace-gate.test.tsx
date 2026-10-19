import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { WorkspaceGate } from "./workspace-gate";

const state = vi.hoisted(() => ({
  pathname: "/",
  value: {} as Record<string, unknown>,
}));

vi.mock("next/navigation", () => ({ usePathname: () => state.pathname }));
vi.mock("@/lib/workspace", () => ({ useWorkspace: () => state.value }));
vi.mock("@/components/shell/page-header", () => ({
  PageHeader: ({ title }: { title: string }) => <h1>{title}</h1>,
  PageBody: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

const me = { user: { github_login: "ada" }, workspaces: [] };

describe("WorkspaceGate", () => {
  beforeEach(() => {
    state.pathname = "/";
    state.value = { me, workspaces: [], isLoading: false, error: null, refetch: vi.fn() };
  });

  it("says plainly when the person is in no workspace", () => {
    render(<WorkspaceGate>page</WorkspaceGate>);
    expect(screen.getByText("You are not in a workspace")).toBeInTheDocument();
    expect(screen.getByText("@ada")).toBeInTheDocument();
    expect(screen.queryByText("page")).not.toBeInTheDocument();
  });

  it("renders the page while loading and once there is a workspace", () => {
    state.value = { ...state.value, me: undefined, isLoading: true };
    const { rerender } = render(<WorkspaceGate>page</WorkspaceGate>);
    expect(screen.getByText("page")).toBeInTheDocument();
    state.value = { ...state.value, me, isLoading: false, workspaces: [{ id: "ws-1" }] };
    rerender(<WorkspaceGate>page</WorkspaceGate>);
    expect(screen.getByText("page")).toBeInTheDocument();
  });

  it("keeps Settings reachable", () => {
    state.pathname = "/settings";
    render(<WorkspaceGate>settings</WorkspaceGate>);
    expect(screen.getByText("settings")).toBeInTheDocument();
  });
});
