import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { plan } from "@/tests/deploy-fixtures";
import { approval } from "@/tests/fixtures";

import { DeployPlanCard } from "./plan-card";

const pending = approval({ tool_name: "k8s_apply" });

describe("DeployPlanCard", () => {
  it("shows the plan with secrets masked and plain values shown", async () => {
    render(<DeployPlanCard plan={plan()} approval={pending} onDecide={vi.fn()} deciding={null} />);
    expect(screen.getByText("Deploy legacy-flask-app to Kubernetes")).toBeInTheDocument();
    expect(screen.getByText("kind-registry:5000/legacy-flask-app:abc")).toBeInTheDocument();
    expect(screen.getByText("gc-legacy-flask-app")).toBeInTheDocument();
    const row = screen.getByText("DATABASE_URL").closest("tr")!;
    expect(row).toHaveTextContent("********");
    expect(row).toHaveTextContent("secret, required");
    expect(screen.getByText("production")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /show the rendered manifests/i }));
    expect(screen.getByText(/DATABASE_URL: "\*\*\*\*\*\*\*\*"/)).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("hunter2");
  });

  it("calls onDecide with the decision", async () => {
    const onDecide = vi.fn();
    render(<DeployPlanCard plan={plan()} approval={pending} onDecide={onDecide} deciding={null} />);
    await userEvent.click(screen.getByRole("button", { name: /approve and deploy/i }));
    await userEvent.click(screen.getByRole("button", { name: /reject/i }));
    expect(onDecide.mock.calls).toEqual([["approve"], ["reject"]]);
  });

  it("blocks approval while a preflight check fails, but allows rejecting", async () => {
    const onDecide = vi.fn();
    const failing = plan({
      preflight: [{ name: "env", status: "fail", detail: "Missing required: DATABASE_URL", overridden: false }],
    });
    render(<DeployPlanCard plan={failing} approval={pending} onDecide={onDecide} deciding={null} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/cannot be approved/);
    expect(screen.getByRole("button", { name: /approve and deploy/i })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: /reject/i }));
    expect(onDecide).toHaveBeenCalledWith("reject");
  });

  it("allows an overridden failure and says so", () => {
    const overridden = plan({
      allow_missing_env: true,
      preflight: [{ name: "env", status: "fail", detail: "Missing required: DATABASE_URL", overridden: true }],
    });
    render(<DeployPlanCard plan={overridden} approval={pending} onDecide={vi.fn()} deciding={null} />);
    expect(screen.getByRole("button", { name: /approve and deploy/i })).toBeEnabled();
    expect(screen.getByText(/allowed: deploy with missing env/)).toBeInTheDocument();
  });

  it("labels a rollback as one", () => {
    render(
      <DeployPlanCard
        plan={plan({ rollback_to_revision: "3" })}
        approval={approval({ policy: "emergency" })}
        onDecide={vi.fn()}
        deciding={null}
      />,
    );
    expect(screen.getByRole("region", { name: "Rollback approval" })).toBeInTheDocument();
    expect(screen.getByText(/Goes back to revision 3/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /approve rollback/i })).toBeInTheDocument();
    expect(screen.getByText("emergency")).toBeInTheDocument();
  });

  it("shows who decided instead of buttons", () => {
    const decided = approval({ status: "approved", decided_by_name: "Alice Admin", decided_at: new Date().toISOString() });
    render(<DeployPlanCard plan={plan()} approval={decided} onDecide={vi.fn()} deciding={null} />);
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
    expect(screen.getByText("Alice Admin")).toBeInTheDocument();
  });
});
