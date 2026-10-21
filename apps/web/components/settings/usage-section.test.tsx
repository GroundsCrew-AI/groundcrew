import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { UsageList } from "./usage-section";

const usage = {
  day: "2026-10-21",
  resets_at: "2026-10-22T00:00:00Z",
  usd_today: 0.12,
  daily_usd_cap: 0.5,
  platform_paused: false,
  max_build_seconds: 900,
  quotas: [
    { name: "tavily", label: "Web research calls today", used: 10, limit: 10, daily: true },
    { name: "builds", label: "Deploy builds today", used: 2, limit: 10, daily: true },
  ],
};

describe("UsageList", () => {
  it("shows each limit and marks the ones reached", () => {
    render(<UsageList usage={usage} />);
    expect(screen.getByText("$0.12 of $0.50")).toBeInTheDocument();
    expect(screen.getByText("10 of 10")).toHaveClass("font-medium");
    expect(screen.getByText("2 of 10")).not.toHaveClass("font-medium");
    expect(screen.getByRole("meter", { name: "Deploy builds today" })).toHaveAttribute("aria-valuenow", "2");
    expect(screen.getByText(/up to 15 minutes/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says when Groundcrew itself is paused", () => {
    render(<UsageList usage={{ ...usage, platform_paused: true }} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/paused for every workspace/);
  });
});
