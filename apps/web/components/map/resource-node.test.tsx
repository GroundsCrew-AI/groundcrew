import type { NodeProps } from "@xyflow/react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { node } from "@/tests/discovery-fixtures";

import { ResourceNode, type ResourceFlowNode } from "./resource-node";

// Handles need a React Flow store; the node's own behavior does not.
vi.mock("@xyflow/react", () => ({ Handle: () => null, Position: { Left: "left", Right: "right" } }));

function renderNode(selected = false) {
  const onOpen = vi.fn();
  const props = { data: { resource: node("ecs-1", { name: "checkout-api" }), selected, dimmed: false, onOpen } };
  render(<ResourceNode {...(props as unknown as NodeProps<ResourceFlowNode>)} />);
  return onOpen;
}

describe("ResourceNode", () => {
  it("is reachable with Tab and opens its panel with Enter or Space", async () => {
    const onOpen = renderNode();
    await userEvent.tab();
    const button = screen.getByRole("button", { name: /checkout-api/ });
    expect(button).toHaveFocus();
    expect(button).toHaveAttribute("aria-expanded", "false");
    await userEvent.keyboard("{Enter}");
    expect(onOpen).toHaveBeenLastCalledWith("ecs-1");
    await userEvent.keyboard(" ");
    expect(onOpen).toHaveBeenCalledTimes(2);
  });

  it("opens on click and says when its panel is open", async () => {
    const onOpen = renderNode(true);
    const button = screen.getByRole("button", { name: /checkout-api/ });
    expect(button).toHaveAttribute("aria-expanded", "true");
    await userEvent.click(button);
    expect(onOpen).toHaveBeenCalledWith("ecs-1");
  });
});
