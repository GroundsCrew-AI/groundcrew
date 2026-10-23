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
  useActivity: () => ({ data: activity }),
}));
vi.mock("@/lib/deploys", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/deploys")>()),
  useDeploys: () => ({
    isLoading: false,
    data: {
      items: [
        {
          id: "d1",
          status: "live",
          app: "checkout-demo-api",
          source: { repo: "acme/checkout-demo" },
          target: { name: "kind" },
          created_at: new Date().toISOString(),
        },
      ],
    },
  }),
}));
vi.mock("@/lib/discovery", () => ({
  useConnections: () => ({
    isLoading: false,
    data: [
      { id: "c1", kind: "aws", name: "prod", status: "connected", last_discovery: { at: new Date().toISOString(), resources: 41 } },
      { id: "c2", kind: "agent", name: "laptop", status: "error", last_discovery: null },
    ],
  }),
}));

const day = (d: string, runs: Record<string, number>, usd = 0) => ({
  day: d,
  runs,
  succeeded: Object.values(runs).reduce((a, b) => a + b, 0),
  failed: 0,
  usd,
  tokens: 0,
  incidents_opened: 0,
  incidents_resolved: 0,
});
const activity = {
  days: [day("2026-10-21", {}), day("2026-10-22", { discovery: 2, medic: 1 }, 0.2), day("2026-10-23", { deploy: 1, echo_ops: 1 }, 0.1)],
  agents: [
    { agent: "discovery", runs: 2, succeeded: 2, failed: 0, tokens: 100, median_seconds: 2.5 },
    { agent: "deploy", runs: 4, succeeded: 3, failed: 1, tokens: 900, median_seconds: 600 },
  ],
  open_by_severity: { high: 2 },
  median_resolve_seconds: 3600,
};
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

  it("charts agent activity and spend, and lists agents, incidents, deploys, and connections", () => {
    render(<OverviewHome />);
    const runs = screen.getByRole("list", { name: "Runs per day by agent" });
    expect(screen.getAllByRole("listitem", { name: /^Oct 2[123]:/ }).length).toBeGreaterThanOrEqual(3);
    expect(runs).toBeInTheDocument();
    expect(screen.getByRole("listitem", { name: "Oct 22: 3 total, Discovery 2, Medic 1" })).toBeInTheDocument();
    // Other agents fold into "Other"; legend only for series with runs.
    expect(screen.getByRole("listitem", { name: "Oct 23: 2 total, Deploy 1, Other 1" })).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Model spend per day in USD" })).toBeInTheDocument();
    expect(screen.getByRole("meter", { name: "Deploy success rate" })).toHaveAttribute("aria-valuetext", "75% succeeded");
    expect(screen.getByRole("link", { name: "High 2" })).toHaveAttribute("href", "/incidents");
    expect(screen.getByText("1h 0m")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /checkout-demo-api/ })).toHaveAttribute("href", "/deploy/d1");
    expect(screen.getByText("1 of 2 connected · 41 resources found")).toBeInTheDocument();
  });
});
