import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";

import ApprovalsPage from "@/app/(app)/approvals/page";

vi.mock("next/navigation", () => ({
  usePathname: () => "/approvals",
  // Read the URL the page wrote with the History API, as the App Router does.
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
vi.mock("@/lib/workspace", () => ({ useWorkspace: () => ({ workspace: { id: "ws-1" } }) }));
vi.mock("@/lib/queries", () => ({
  useApprovals: () => ({ data: [], isLoading: false, error: null }),
  useDecide: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/components/shell/page-header", () => ({
  PageHeader: () => null,
  PageBody: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
afterAll(() => window.history.replaceState(null, "", "/"));

describe("ApprovalsPage", () => {
  beforeEach(() => window.history.replaceState(null, "", "/approvals"));

  it("opens on the tab in the URL", () => {
    window.history.replaceState(null, "", "/approvals?tab=decided");
    render(<ApprovalsPage />);
    expect(screen.getByRole("tab", { name: "Decided" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("No decisions yet")).toBeInTheDocument();
  });

  it("writes the tab to the URL, so Back returns to the last one", async () => {
    const push = vi.spyOn(window.history, "pushState");
    const { rerender } = render(<ApprovalsPage />);
    expect(screen.getByText("Nothing to approve")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Decided" }));
    expect(push).toHaveBeenLastCalledWith(null, "", "/approvals?tab=decided");
    rerender(<ApprovalsPage />);
    expect(screen.getByText("No decisions yet")).toBeInTheDocument();

    // Back: the URL has no tab again, and the page follows it.
    window.history.replaceState(null, "", "/approvals");
    rerender(<ApprovalsPage />);
    expect(screen.getByRole("tab", { name: /Pending/ })).toHaveAttribute("aria-selected", "true");
    push.mockRestore();
  });
});
