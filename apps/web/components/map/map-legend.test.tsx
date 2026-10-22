import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { MapLegend } from "./map-legend";

// The panel only positions its children inside React Flow.
vi.mock("@xyflow/react", () => ({ Panel: ({ children }: { children: ReactNode }) => <div>{children}</div> }));

describe("MapLegend", () => {
  it("opens, says what each line means, and hides a kind of line", async () => {
    const onToggle = vi.fn();
    render(<MapLegend kinds={["calls", "pulls_from"]} hidden={new Set(["calls"])} onToggle={onToggle} />);
    const toggle = screen.getByRole("button", { name: /Legend/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(toggle);
    expect(screen.getByRole("button", { name: "calls" })).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(screen.getByRole("button", { name: "pulls image" }));
    expect(onToggle).toHaveBeenCalledWith("pulls_from");
    expect(screen.getByText("Actionable")).toBeInTheDocument();
  });
});
