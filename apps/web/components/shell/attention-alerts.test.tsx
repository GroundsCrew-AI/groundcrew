import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AttentionAlerts, attentionAlerts, titleWithCount } from "./attention-alerts";

const state = vi.hoisted(() => ({
  pathname: "/runs",
  workspace: { id: "ws-1" } as { id: string } | undefined,
  summary: undefined as { pending_approvals: number; open_incidents: number } | undefined,
}));
const toast = vi.hoisted(() => Object.assign(vi.fn(), { dismiss: vi.fn() }));

vi.mock("sonner", () => ({ toast }));
vi.mock("next/navigation", () => ({ usePathname: () => state.pathname }));
vi.mock("@/lib/workspace", () => ({ useWorkspace: () => ({ workspace: state.workspace }) }));
vi.mock("@/lib/queries", () => ({ useSummary: () => ({ data: state.summary }) }));

describe("attentionAlerts", () => {
  it("speaks only about counts that went up", () => {
    expect(attentionAlerts({ approvals: 1, incidents: 2 }, { approvals: 1, incidents: 1 })).toEqual([]);
    expect(attentionAlerts({ approvals: 0, incidents: 0 }, { approvals: 1, incidents: 2 })).toEqual([
      { kind: "approvals", added: 1, href: "/approvals", message: "An action is waiting for your approval" },
      { kind: "incidents", added: 2, href: "/incidents", message: "2 new incidents are open" },
    ]);
  });
});

describe("titleWithCount", () => {
  it("adds, replaces, caps, and removes the count", () => {
    expect(titleWithCount("Runs | Groundcrew", 3)).toBe("(3) Runs | Groundcrew");
    expect(titleWithCount("(3) Runs | Groundcrew", 4)).toBe("(4) Runs | Groundcrew");
    expect(titleWithCount("Runs | Groundcrew", 120)).toBe("(99+) Runs | Groundcrew");
    expect(titleWithCount("(99+) Runs | Groundcrew", 0)).toBe("Runs | Groundcrew");
  });
});

describe("AttentionAlerts", () => {
  beforeEach(() => {
    toast.mockReset();
    state.pathname = "/runs";
    state.workspace = { id: "ws-1" };
    state.summary = { pending_approvals: 0, open_incidents: 0 };
    document.title = "Runs | Groundcrew";
  });
  afterEach(() => {
    document.title = "";
  });

  it("takes the first counts as the baseline, then toasts what is new", () => {
    const { rerender } = render(<AttentionAlerts />);
    expect(toast).not.toHaveBeenCalled();
    state.summary = { pending_approvals: 1, open_incidents: 0 };
    rerender(<AttentionAlerts />);
    expect(toast).toHaveBeenCalledWith("An action is waiting for your approval", expect.objectContaining({ id: "attention-approvals" }));
    expect(document.title).toBe("(1) Runs | Groundcrew");
  });

  it("stays quiet on the page that already shows the new item", () => {
    state.pathname = "/incidents";
    const { rerender } = render(<AttentionAlerts />);
    state.summary = { pending_approvals: 0, open_incidents: 1 };
    rerender(<AttentionAlerts />);
    expect(toast).not.toHaveBeenCalled();
  });

  it("does not toast when the workspace changes", () => {
    const { rerender } = render(<AttentionAlerts />);
    state.workspace = { id: "ws-2" };
    state.summary = { pending_approvals: 4, open_incidents: 2 };
    rerender(<AttentionAlerts />);
    expect(toast).not.toHaveBeenCalled();
  });
});
