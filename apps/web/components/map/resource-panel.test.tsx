import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { detail } from "@/tests/discovery-fixtures";

import { ResourceDetails } from "./resource-panel";
import { SystemSummary } from "./system-summary";

describe("ResourceDetails", () => {
  it("shows config, the IaC link, edges with evidence, and deploys", () => {
    render(<ResourceDetails detail={detail()} onSelect={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "checkout-api" })).toBeInTheDocument();
    expect(screen.getByText("Diagnosable")).toBeInTheDocument();
    expect(screen.getByText("3.15.10.20")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /infra\/aws\/ecs.tf#L94/ })).toHaveAttribute(
      "href",
      "https://github.com/GroundsCrew-AI/checkout-demo/blob/HEAD/infra/aws/ecs.tf#L94",
    );
    expect(screen.getByText(/allows port 5432 from checkout-tasks/)).toBeInTheDocument();
    expect(screen.getByText(/value not read/)).toBeInTheDocument();
    expect(screen.getByText("Medium")).toBeInTheDocument();
    expect(screen.getByText("cef79e7")).toBeInTheDocument();
  });

  it("selects the other side of an edge", async () => {
    const onSelect = vi.fn();
    render(<ResourceDetails detail={detail()} onSelect={onSelect} />);
    await userEvent.click(screen.getByRole("button", { name: "checkout-orders-db" }));
    expect(onSelect).toHaveBeenCalledWith("db");
  });
});

describe("SystemSummary", () => {
  it("shows bullets and rule risks", () => {
    render(
      <SystemSummary
        summary={{
          text: "- checkout-api and checkout-worker run on ECS\n- Risk: model wording",
          risks: ["Risk: checkout-api has no load balancer or stable endpoint."],
          generated_at: new Date().toISOString(),
          run_id: "run-1",
          error: null,
        }}
      />,
    );
    expect(screen.getByText("checkout-api and checkout-worker run on ECS")).toBeInTheDocument();
    expect(screen.getByText("checkout-api has no load balancer or stable endpoint.")).toBeInTheDocument();
    expect(screen.queryByText("model wording")).not.toBeInTheDocument();
  });
});
