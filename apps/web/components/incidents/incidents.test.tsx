import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Timeline } from "@/components/runs/timeline";
import { isMedicStep } from "@/lib/incidents";
import { approval } from "@/tests/fixtures";
import { EVIDENCE, incidentSummary, medicStep, PR_FIX, ROLLBACK, ROOT_CAUSE } from "@/tests/incident-fixtures";

import { position } from "./change-strip";
import { FixApprovalCard } from "./fix-approval-card";
import { IncidentList } from "./incident-list";
import { MedicStep } from "./medic-step";
import { RootCausePanel } from "./root-cause-panel";

describe("FixApprovalCard", () => {
  it("shows an emergency action as before and after", async () => {
    const onDecide = vi.fn();
    render(<FixApprovalCard fix={ROLLBACK} approval={approval({ policy: "emergency" })} onDecide={onDecide} deciding={null} />);
    expect(screen.getByText("Roll back checkout-worker to checkout-worker:41")).toBeInTheDocument();
    expect(screen.getByText("checkout-worker:42")).toBeInTheDocument();
    expect(screen.getByText("checkout-worker:41")).toBeInTheDocument();
    expect(screen.getByText("Changes production now.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /approve and run/i }));
    expect(onDecide).toHaveBeenCalledWith("approve");
  });

  it("shows a pull request as its diff and says it is not merged", () => {
    render(<FixApprovalCard fix={PR_FIX} approval={approval({ id: "approval-2" })} onDecide={vi.fn()} deciding={null} />);
    expect(screen.getByText("worker/ecs-task.json")).toBeInTheDocument();
    expect(screen.getByText('+{"memory": "512"}')).toBeInTheDocument();
    expect(screen.getByText(/It is not merged/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /approve and open pr/i })).toBeInTheDocument();
  });

  it("shows who decided instead of buttons", () => {
    const decided = approval({ status: "rejected", decided_by_name: "Ahmed", decided_at: new Date().toISOString() });
    render(<FixApprovalCard fix={ROLLBACK} approval={decided} onDecide={vi.fn()} deciding={null} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByText("Rejected")).toBeInTheDocument();
  });
});

describe("RootCausePanel", () => {
  it("opens a claim to the exact evidence lines it cites", async () => {
    render(<RootCausePanel rootCause={ROOT_CAUSE} evidence={EVIDENCE} />);
    expect(screen.getByText(ROOT_CAUSE.summary)).toBeInTheDocument();
    expect(screen.getByText(/Started by:/).parentElement).toHaveTextContent("Started by: Commit 9f8e7d6");
    expect(screen.queryByText(/OutOfMemoryError/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Worker tasks exit 137/ }));
    expect(screen.getByText(/OutOfMemoryError: Container killed/)).toBeInTheDocument();
  });
});

describe("MedicStep", () => {
  it("renders the diagnosis and how many uncited items were dropped", () => {
    render(<MedicStep step={medicStep(5, { type: "diagnosis", root_cause: ROOT_CAUSE, dropped: ["uncited claim: x"] })} />);
    expect(screen.getByText(ROOT_CAUSE.summary)).toBeInTheDocument();
    expect(screen.getByText("1 uncited item dropped")).toBeInTheDocument();
  });

  it("links a memory hit to the past incident", () => {
    const data = {
      type: "memory_checked" as const,
      hits: [{ incident_id: "inc-0", title: "Worker OOM last week", similarity: 0.93, root_cause: null, fix_summary: null, resolved_at: null }],
      reused_incident_id: "inc-0",
      reused: ["diagnosis" as const, "fix" as const],
    };
    render(<MedicStep step={medicStep(3, data)} />);
    expect(screen.getByRole("link", { name: "Worker OOM last week" })).toHaveAttribute("href", "/incidents/inc-0");
    expect(screen.getByText(/93% similar/)).toBeInTheDocument();
  });

  it("links an opened fix PR and lists what still fails", () => {
    const pr = { repo: "GroundsCrew-AI/checkout-demo", number: 7, url: "https://github.com/x/pull/7", branch: "groundcrew/fix-a", head_sha: "h", state: "open" as const };
    const { rerender } = render(<MedicStep step={medicStep(9, { type: "fix_applied", fix_id: "fix-2", kind: "pr", ok: true, pull_request: pr, action_result: null, error: null })} />);
    expect(screen.getByRole("link", { name: /checkout-demo#7/ })).toHaveAttribute("href", pr.url);
    rerender(
      <MedicStep step={medicStep(12, { type: "incident_failed", incident_id: "inc-1", reason: "Signals did not recover.", still_failing: ["checkout-worker: 0/1 tasks running"], cost_usd: null })} />,
    );
    expect(screen.getByText("Not resolved")).toBeInTheDocument();
    expect(screen.getByText("checkout-worker: 0/1 tasks running")).toBeInTheDocument();
  });
});

describe("Timeline with a Medic run", () => {
  it("routes incident events to Medic and fix approvals to the fix card", () => {
    const steps = [
      medicStep(1, { type: "diagnosis", root_cause: ROOT_CAUSE }),
      medicStep(2, { type: "fix_approval", incident_id: "inc-1", fix: ROLLBACK }, { kind: "approval_requested", approval_id: "approval-1" }),
    ];
    expect(steps.every(isMedicStep)).toBe(true);
    render(
      <Timeline steps={steps} approvals={{ "approval-1": approval({ policy: "emergency" }) }} status="waiting_approval"
        result={null} onDecide={vi.fn()} deciding={null} runAgent="medic" />,
    );
    expect(screen.getByText("Root cause")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: /Approval for Roll back checkout-worker/ })).toBeInTheDocument();
  });
});

describe("IncidentList", () => {
  it("shows status, severity, resources, and the detector", () => {
    render(<IncidentList incidents={[incidentSummary(), incidentSummary({ id: "inc-2", status: "resolved", detected_by: "user", duration_ms: 420000 })]} />);
    expect(screen.getByText("Needs approval")).toBeInTheDocument();
    expect(screen.getByText("Resolved")).toBeInTheDocument();
    expect(screen.getByText("Tasks crashing")).toBeInTheDocument();
    expect(screen.getByText("A person")).toBeInTheDocument();
    expect(screen.getAllByRole("link")[0]).toHaveAttribute("href", "/incidents/inc-1");
  });
});

describe("change strip", () => {
  it("places changes inside the window", () => {
    expect(position("2026-10-12T10:00:00Z", "2026-10-12T09:30:00Z", "2026-10-12T10:30:00Z")).toBe(50);
    expect(position("2026-10-12T08:00:00Z", "2026-10-12T09:30:00Z", "2026-10-12T10:30:00Z")).toBe(0);
  });
});
