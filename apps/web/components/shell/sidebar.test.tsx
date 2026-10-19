import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SidebarContent } from "./sidebar";

const summary = vi.hoisted(() => ({ data: undefined as { pending_approvals: number; open_incidents: number } | undefined }));

vi.mock("@/app/actions", () => ({ signOutAction: vi.fn() }));
vi.mock("next/navigation", () => ({ usePathname: () => "/" }));
vi.mock("next-themes", () => ({ useTheme: () => ({ theme: "dark", setTheme: vi.fn() }) }));
vi.mock("@/lib/queries", () => ({ useSummary: () => summary }));
vi.mock("@/lib/workspace", () => ({
  useWorkspace: () => ({
    me: { user: { name: "Ada", github_login: "ada", avatar_url: null } },
    workspace: { id: "ws-1", name: "Acme", role: "owner" },
    workspaces: [{ id: "ws-1", name: "Acme", role: "owner" }],
    setWorkspaceId: vi.fn(),
    isLoading: false,
  }),
}));

describe("SidebarContent", () => {
  beforeEach(() => {
    summary.data = undefined;
  });

  it("shows open incidents and pending approvals as badges", () => {
    summary.data = { pending_approvals: 1, open_incidents: 3 };
    render(<SidebarContent collapsed={false} />);
    expect(screen.getByRole("link", { name: "Incidents, 3 open" })).toHaveAttribute("href", "/incidents");
    expect(screen.getByRole("link", { name: "Approvals, 1 pending" })).toHaveAttribute("href", "/approvals");
  });

  it("shows no badge at zero", () => {
    summary.data = { pending_approvals: 0, open_incidents: 0 };
    render(<SidebarContent collapsed={false} />);
    expect(screen.getByRole("link", { name: "Incidents" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Approvals" })).toBeInTheDocument();
  });

  it("caps large counts", () => {
    summary.data = { pending_approvals: 0, open_incidents: 140 };
    render(<SidebarContent collapsed={false} />);
    expect(screen.getByText("99+")).toBeInTheDocument();
  });
});
