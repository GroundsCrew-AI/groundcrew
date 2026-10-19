import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { OverviewHome } from "@/app/(app)/overview-home";

vi.mock("@/lib/workspace", () => ({
  useWorkspace: () => ({ workspace: { id: "ws-1", role: "owner" }, error: null, refetch: vi.fn() }),
}));
vi.mock("@/lib/queries", () => ({
  useSummary: () => ({
    data: {
      runs_today: 4,
      pending_approvals: 2,
      workspace_tokens_today: 12_345,
      budget_tokens_today: 0,
      daily_token_budget: 500_000,
      usd_today: 0.5,
      daily_usd_cap: 2,
      usd_total: 7.25,
      total_usd_cap: null,
      pending_permanent_fixes: 1,
      open_incidents: 3,
    },
    error: null,
  }),
  useRuns: () => ({ data: { pages: [{ items: [] }] }, isLoading: false, error: null }),
}));
vi.mock("@/components/overview/getting-started", () => ({ GettingStarted: () => null }));
vi.mock("@/components/shell/page-header", () => ({
  PageHeader: ({ title, actions }: { title: string; actions?: React.ReactNode }) => (
    <header>
      <h1>{title}</h1>
      {actions}
    </header>
  ),
  PageBody: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

describe("OverviewHome", () => {
  it("shows open incidents, approvals, spend against the cap, and runs", () => {
    render(<OverviewHome />);
    const at = screen.getByRole("region", { name: "At a glance" });
    expect(at).toHaveTextContent("Open incidents3");
    expect(screen.getByRole("link", { name: /Open incidents/ })).toHaveAttribute("href", "/incidents");
    expect(screen.getByRole("link", { name: /Waiting for approval/ })).toHaveAttribute("href", "/approvals");
    expect(screen.getByText("Model spend today")).toBeInTheDocument();
    expect(screen.getByText("$0.50 of $2.00 daily cap")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "25% of today's spend cap used" })).toBeInTheDocument();
    expect(screen.getByText("12,345 tokens today")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /1 incident has a permanent fix pending/ })).toHaveAttribute(
      "href",
      "/incidents?pending_fix=true",
    );
  });

  it("does not push the test agent or show env var names", () => {
    render(<OverviewHome />);
    expect(screen.queryByText(/Echo Ops|test run|DAILY_TOKEN_BUDGET|shared daily budget/i)).not.toBeInTheDocument();
    expect(screen.getByText("Nothing has run yet")).toBeInTheDocument();
  });
});
