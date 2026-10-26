import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { IncidentDetail } from "@/lib/incidents";
import { incidentSummary, PR_FIX, ROLLBACK } from "@/tests/incident-fixtures";

import { Fixes } from "./fix-list";

const DENIED =
  "AccessDeniedException: groundcrew-operate is not authorized to perform: iam:PassRole on resource: " +
  "arn:aws:iam::881050425883:role/checkout-worker-task-execution";

function incident(overrides: Partial<IncidentDetail> = {}): IncidentDetail {
  return {
    ...incidentSummary({ status: "verifying" }),
    signals: [],
    evidence: [],
    changes: [],
    root_cause: null,
    fixes: [
      {
        fix: ROLLBACK,
        status: "failed",
        approval_id: "approval-1",
        applied: { type: "fix_applied", fix_id: ROLLBACK.fix_id, kind: "emergency", ok: false, error: DENIED, action_result: null, pull_request: null },
      },
      { fix: PR_FIX, status: "applied", approval_id: "approval-2" },
    ],
    verify: null,
    outcome: null,
    memory: null,
    research: [],
    run_ids: ["run-1"],
    ...overrides,
  };
}

describe("Fixes", () => {
  it("shows why an emergency fix failed and retries it", async () => {
    const onRetry = vi.fn();
    render(<Fixes incident={incident()} onRetry={onRetry} retrying={null} retryError={null} />);
    expect(screen.getByText(`Failed: ${DENIED}`)).toBeInTheDocument();
    expect(screen.getByText(/asks for a new approval/)).toBeInTheDocument();
    const buttons = screen.getAllByRole("button", { name: "Retry" });
    expect(buttons).toHaveLength(1); // the applied PR has no retry
    await userEvent.click(buttons[0]!);
    expect(onRetry).toHaveBeenCalledWith(ROLLBACK.fix_id);
  });

  it("says when an approved restart was not needed", () => {
    const summary = "Not needed: checkout-api runs again; nothing was sent to the agent.";
    const skipped = incident();
    skipped.fixes[0]!.status = "applied";
    skipped.fixes[0]!.applied = {
      ...skipped.fixes[0]!.applied!,
      ok: true,
      error: null,
      action_result: {
        action: "agent_restart_service",
        ok: true,
        summary,
        details: { skipped: "checkout-api running" },
        error: null,
        started_at: "2026-10-26T09:22:00Z",
        finished_at: "2026-10-26T09:22:00Z",
      },
    };
    render(<Fixes incident={skipped} onRetry={vi.fn()} retrying={null} retryError={null} />);
    expect(screen.getByText(summary)).toBeInTheDocument();
    expect(screen.queryByText(/^Failed:/)).not.toBeInTheDocument();
  });

  it("shows why a retry could not be planned, and no retry once resolved", () => {
    const why = "rollback needs PassRole on arn:aws:iam::881050425883:role/checkout-worker-task; update the connection";
    const { rerender } = render(
      <Fixes incident={incident()} onRetry={vi.fn()} retrying={ROLLBACK.fix_id} retryError={{ fixId: ROLLBACK.fix_id, message: why }} />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(why);
    expect(screen.getByRole("button", { name: "Planning" })).toBeDisabled();
    rerender(<Fixes incident={incident({ status: "resolved" })} onRetry={vi.fn()} retrying={null} retryError={null} />);
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });
});
