import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { approval } from "@/tests/fixtures";

import { ApprovalCard } from "./approval-card";

describe("ApprovalCard", () => {
  it("shows the exact action and its policy", () => {
    render(<ApprovalCard approval={approval()} onDecide={vi.fn()} deciding={null} />);
    expect(screen.getByText("Approval needed")).toBeInTheDocument();
    expect(screen.getByText("restart_fake_service")).toBeInTheDocument();
    expect(screen.getByText(/"name": "api"/)).toBeInTheDocument();
    expect(screen.getByText("write")).toBeInTheDocument();
  });

  it("calls onDecide with the decision", async () => {
    const onDecide = vi.fn();
    render(<ApprovalCard approval={approval()} onDecide={onDecide} deciding={null} />);
    await userEvent.click(screen.getByRole("button", { name: /approve/i }));
    await userEvent.click(screen.getByRole("button", { name: /reject/i }));
    expect(onDecide.mock.calls).toEqual([["approve"], ["reject"]]);
  });

  it("disables both buttons while a decision is being sent", () => {
    render(<ApprovalCard approval={approval()} onDecide={vi.fn()} deciding="approve" />);
    expect(screen.getByRole("button", { name: /approving/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /reject/i })).toBeDisabled();
  });

  it("shows who decided instead of buttons once decided", () => {
    const decided = approval({
      status: "approved",
      decided_by_name: "Alice Admin",
      decided_by_login: "alice",
      decided_at: new Date().toISOString(),
    });
    render(<ApprovalCard approval={decided} onDecide={vi.fn()} deciding={null} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByText("Approved")).toBeInTheDocument();
    expect(screen.getByText("Alice Admin")).toBeInTheDocument();
  });

  it("offers no buttons when the run ended before a decision", () => {
    render(<ApprovalCard approval={approval()} onDecide={vi.fn()} deciding={null} runEnded />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByText(/run ended before anyone decided/i)).toBeInTheDocument();
  });
});
