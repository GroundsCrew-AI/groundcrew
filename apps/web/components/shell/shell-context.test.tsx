import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Button } from "@/components/ui/button";

import { ShellProvider, useAnnounceChange } from "./shell-context";

function Status({ text }: { text?: string }) {
  useAnnounceChange(text);
  return null;
}

describe("useAnnounceChange", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("reads status changes through the shell's live region, not the first value", () => {
    const { rerender } = render(
      <ShellProvider>
        <Status text="Run status: Running" />
      </ShellProvider>,
    );
    const region = screen.getByRole("status");
    expect(region).toHaveAttribute("aria-live", "polite");
    act(() => void vi.runAllTimers());
    expect(region).toHaveTextContent("");

    rerender(
      <ShellProvider>
        <Status text="Run status: Needs approval" />
      </ShellProvider>,
    );
    act(() => void vi.runAllTimers());
    expect(region).toHaveTextContent("Run status: Needs approval");
  });

  it("says nothing when the page finishes loading", () => {
    const { rerender } = render(
      <ShellProvider>
        <Status />
      </ShellProvider>,
    );
    rerender(
      <ShellProvider>
        <Status text="Incident status: Investigating" />
      </ShellProvider>,
    );
    act(() => void vi.runAllTimers());
    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  it("is a no-op outside the shell", () => {
    expect(() => render(<Status text="x" />)).not.toThrow();
  });
});

describe("Button", () => {
  it("has a solid destructive variant with its own foreground token", () => {
    render(<Button variant="destructive-solid">Remove</Button>);
    expect(screen.getByRole("button", { name: "Remove" }).className).toContain("text-destructive-foreground");
  });
});
