import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { Summary } from "@/lib/api/client";

import { formatUsd, SpendMeter, spendOf, spendTone, usedPercent } from "./usage";

function summary(overrides: Partial<Summary> = {}): Summary {
  return {
    runs_today: 3,
    pending_approvals: 0,
    workspace_tokens_today: 1200,
    budget_tokens_today: 5000,
    daily_token_budget: null,
    usd_today: 0.12,
    daily_usd_cap: 2,
    usd_total: 4.31,
    total_usd_cap: null,
    pending_permanent_fixes: 0,
    open_incidents: 0,
    ...overrides,
  };
}

describe("usedPercent", () => {
  it("rounds, clamps, and treats a zero cap as full", () => {
    expect(usedPercent(0.5, 2)).toBe(25);
    expect(usedPercent(3, 2)).toBe(100);
    expect(usedPercent(-1, 2)).toBe(0);
    expect(usedPercent(0, 0)).toBe(100);
  });
});

describe("formatUsd", () => {
  it("shows cents, small amounts, and zero", () => {
    expect(formatUsd(1.2)).toBe("$1.20");
    expect(formatUsd(0.0042)).toBe("$0.0042");
    expect(formatUsd(0)).toBe("$0");
    expect(formatUsd(undefined)).toBe("$0");
    expect(formatUsd(null)).toBe("$0");
  });
});

describe("spendOf", () => {
  it("compares today with the cap", () => {
    expect(spendOf(summary())).toEqual({ today: 0.12, total: 4.31, cap: 2, percent: 6, reached: false });
    expect(spendOf(summary({ usd_today: 2 })).reached).toBe(true);
  });

  it("has no percent without a cap, and treats missing amounts as zero", () => {
    expect(spendOf({ daily_usd_cap: null })).toEqual({ today: 0, total: 0, cap: null, percent: null, reached: false });
  });

  it("is reached at once with a zero cap", () => {
    expect(spendOf(summary({ usd_today: 0, daily_usd_cap: 0 }))).toMatchObject({ percent: 100, reached: true });
  });
});

describe("spendTone", () => {
  it("warns from 70% and fails from 90%", () => {
    expect(spendTone(69)).toBe("var(--primary)");
    expect(spendTone(70)).toBe("var(--status-waiting)");
    expect(spendTone(90)).toBe("var(--status-failed)");
  });
});

describe("SpendMeter", () => {
  it("shows today's spend against the daily cap, and the total", () => {
    render(<SpendMeter summary={summary()} />);
    expect(screen.getByText("$0.12 of $2.00 daily cap")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "6% of today's spend cap used" })).toBeInTheDocument();
    expect(screen.getByText("$4.31 in total")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("says when the cap is reached and where to raise it", () => {
    render(<SpendMeter summary={summary({ usd_today: 2.05 })} />);
    expect(screen.getByRole("status")).toHaveTextContent("agents stop calling models until 00:00 UTC");
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings");
  });

  it("says there is no cap in plain words and links to settings", () => {
    render(<SpendMeter summary={summary({ daily_usd_cap: null })} />);
    expect(screen.getByText(/No daily cap/)).toBeInTheDocument();
    expect(screen.queryByText(/DAILY_TOKEN_BUDGET|LLM_/)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Set one in Settings" })).toHaveAttribute("href", "/settings");
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });
});
