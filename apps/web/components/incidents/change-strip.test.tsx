import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ChangeMarker } from "@/lib/incidents";

import { ChangeStrip, collapseRepeats } from "./change-strip";

const VERCEL = "res-vercel";

function change(at: string, label: string, overrides: Partial<ChangeMarker> = {}): ChangeMarker {
  return { kind: "deploy", ref: `${label}-${at}`, at, label, resource_id: VERCEL, url: null, commit: null, repo: null, ...overrides };
}

// Live broken-env 2026-10-14: the API_URL Lambda's redeploys every few minutes, then the secret.
const LIVE = [
  change("2026-10-14T10:43:14Z", "checkout-demo redeploy READY (same source, commit 5f63808)"),
  change("2026-10-14T10:46:35Z", "checkout-demo redeploy READY (same source, commit 5f63808)"),
  change("2026-10-14T10:50:46Z", "checkout-demo redeploy READY (same source, commit 5f63808)"),
  change("2026-10-14T10:57:18Z", "checkout/database-url changed", { kind: "secret_change", resource_id: "res-secret" }),
  change("2026-10-14T10:58:00Z", "checkout-demo redeploy READY (same source, commit 5f63808)"),
];

describe("collapseRepeats", () => {
  it("folds the same change back to back into one row with a count", () => {
    const rows = collapseRepeats(LIVE);
    expect(rows.map((r) => [r.change.label, r.count])).toEqual([
      ["checkout-demo redeploy READY (same source, commit 5f63808)", 3],
      ["checkout/database-url changed", 1],
      ["checkout-demo redeploy READY (same source, commit 5f63808)", 1],
    ]);
    expect(rows[0]!.last).toBe("2026-10-14T10:50:46Z");
  });
});

describe("ChangeStrip", () => {
  it("marks the opening signal's onset, not an earlier signal of a taken-over incident", () => {
    const signals = [
      { rule: "log_error_spike" as const, resource_id: "w", resource_name: "checkout-worker", detail: "6 lines", at: "2026-10-14T10:13:13Z", down: false },
      { rule: "log_error_spike" as const, resource_id: "w", resource_name: "checkout-worker", detail: "9 lines", at: "2026-10-14T10:57:59Z", down: false },
    ];
    render(
      <ChangeStrip
        changes={LIVE}
        windowStart="2026-10-14T09:43:13Z"
        windowEnd="2026-10-14T11:21:20Z"
        signals={signals}
        firstSignalAt="2026-10-14T10:57:59Z"
      />,
    );
    const first = screen.getByText("First signal").closest("li")!;
    expect(first.querySelector("time")).toHaveAttribute("dateTime", "2026-10-14T10:57:59Z");
    expect(screen.getAllByRole("listitem")).toHaveLength(4); // 3 change rows + the first signal
    expect(screen.getByText(/×3, last/)).toBeInTheDocument();
  });
});
