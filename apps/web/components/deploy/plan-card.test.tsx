import { render as rtlRender, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { plan } from "@/tests/deploy-fixtures";
import { approval } from "@/tests/fixtures";

import { DeployPlanCard } from "./plan-card";

const pending = approval({ tool_name: "k8s_apply" });

// Radix positions the tooltip with ResizeObserver, which jsdom does not have.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
};

function render(ui: ReactElement) {
  return rtlRender(<TooltipProvider>{ui}</TooltipProvider>);
}

const missingEnv = () =>
  plan({
    env: [{ name: "DATABASE_URL", secret: true, required: true, provided: false, value: null }],
    preflight: [{ name: "env", status: "fail", detail: "Missing required: DATABASE_URL", overridden: false }],
  });

describe("DeployPlanCard", () => {
  it("shows the plan with secrets masked and plain values shown", async () => {
    render(<DeployPlanCard plan={plan()} approval={pending} onDecide={vi.fn()} deciding={null} />);
    expect(screen.getByText("Deploy legacy-flask-app to Kubernetes")).toBeInTheDocument();
    expect(screen.getAllByText("kind-registry:5000/legacy-flask-app:abc")).toHaveLength(2);
    expect(screen.getByText("gc-legacy-flask-app")).toBeInTheDocument();
    const row = screen.getByText("DATABASE_URL").closest("tr")!;
    expect(row).toHaveTextContent("********");
    expect(row).toHaveTextContent("secret, required");
    expect(screen.getByText("production")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /show the rendered manifests/i }));
    expect(screen.getByText(/DATABASE_URL: "\*\*\*\*\*\*\*\*"/)).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("hunter2");
  });

  it("lists the image push as the first apply step", () => {
    render(<DeployPlanCard plan={plan()} approval={pending} onDecide={vi.fn()} deciding={null} />);
    const steps = screen.getAllByRole("listitem").filter((li) => /create|push/.test(li.textContent ?? ""));
    expect(steps[0]).toHaveTextContent(/^push\s*image/);
    expect(screen.getByText(/Built and smoke-tested \(sha256:/)).toBeInTheDocument();
  });

  it("calls onDecide with the decision", async () => {
    const onDecide = vi.fn();
    render(<DeployPlanCard plan={plan()} approval={pending} onDecide={onDecide} deciding={null} />);
    await userEvent.click(screen.getByRole("button", { name: /approve and deploy/i }));
    await userEvent.click(screen.getByRole("button", { name: /reject/i }));
    expect(onDecide.mock.calls).toEqual([["approve"], ["reject"]]);
  });

  it("blocks approval while a preflight check fails, says why, and allows rejecting", async () => {
    const onDecide = vi.fn();
    render(<DeployPlanCard plan={missingEnv()} approval={pending} onDecide={onDecide} deciding={null} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/cannot be approved/);
    const approve = screen.getByRole("button", { name: /approve and deploy/i });
    expect(approve).toBeDisabled();
    const wrapper = approve.parentElement!;
    expect(wrapper).toHaveClass("cursor-not-allowed");
    await userEvent.hover(wrapper);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Blocked by preflight. Env vars: Missing required: DATABASE_URL");
    await userEvent.click(screen.getByRole("button", { name: /reject/i }));
    expect(onDecide).toHaveBeenCalledWith("reject");
  });

  it("sets a missing value with a masked input", async () => {
    const onFixEnv = vi.fn();
    render(
      <DeployPlanCard plan={missingEnv()} approval={pending} onDecide={vi.fn()} deciding={null} onFixEnv={onFixEnv} />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/DATABASE_URL is required and not set/);
    await userEvent.click(screen.getByRole("button", { name: "Set a value for DATABASE_URL" }));
    const input = screen.getByLabelText("Value for DATABASE_URL");
    expect(input).toHaveAttribute("type", "password");
    await userEvent.type(input, "postgresql://a:hunter2@db/x");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onFixEnv).toHaveBeenCalledWith({
      env: [{ name: "DATABASE_URL", value: "postgresql://a:hunter2@db/x", secret: true }],
      allow_missing_env: false,
    });
    expect(screen.queryByLabelText("Value for DATABASE_URL")).not.toBeInTheDocument();
  });

  it("continues without the missing value", async () => {
    const onFixEnv = vi.fn();
    render(
      <DeployPlanCard plan={missingEnv()} approval={pending} onDecide={vi.fn()} deciding={null} onFixEnv={onFixEnv} />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Continue without DATABASE_URL" }));
    expect(onFixEnv).toHaveBeenCalledWith({ env: [], allow_missing_env: true });
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

  it("collapses a replaced plan to a note", () => {
    const rejected = approval({ status: "rejected", decided_by_name: "Alice Admin", decided_at: new Date().toISOString() });
    render(<DeployPlanCard plan={missingEnv()} approval={rejected} onDecide={vi.fn()} deciding={null} replaced />);
    expect(screen.getByRole("region", { name: "Replaced deploy plan" })).toHaveTextContent(/image was not rebuilt/);
    expect(screen.queryByText("Alice Admin")).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("labels a rollback as one", () => {
    render(
      <DeployPlanCard
        plan={plan({ rollback_to_revision: "3", source_image: null })}
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
