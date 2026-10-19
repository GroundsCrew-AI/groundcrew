import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { IncidentsHome } from "@/app/(app)/incidents/incidents-home";

const useIncidents = vi.fn();
vi.mock("@/lib/workspace", () => ({ useWorkspace: () => ({ workspace: { id: "ws-1" }, error: null, refetch: vi.fn() }) }));
vi.mock("@/components/incidents/report-dialog", () => ({ ReportIncidentDialog: () => null }));
vi.mock("@/components/shell/page-header", () => ({
  PageHeader: () => null,
  PageBody: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
vi.mock("@/lib/incidents", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useIncidents: (...args: unknown[]) => useIncidents(...args),
}));

describe("IncidentsHome", () => {
  beforeEach(() => {
    useIncidents.mockReset();
    useIncidents.mockReturnValue({ data: { pages: [{ items: [] }] }, isLoading: false, error: null });
  });

  it("filters incidents whose permanent fix is pending", async () => {
    render(<IncidentsHome />);
    expect(useIncidents).toHaveBeenLastCalledWith("ws-1", {});
    await userEvent.click(screen.getByRole("tab", { name: "Fix pending" }));
    expect(useIncidents).toHaveBeenLastCalledWith("ws-1", { pending_fix: true });
  });

  it("keeps merged incidents in All and filters them under Merged", async () => {
    render(<IncidentsHome />);
    expect(useIncidents).toHaveBeenLastCalledWith("ws-1", {});
    await userEvent.click(screen.getByRole("tab", { name: "Merged" }));
    expect(useIncidents).toHaveBeenLastCalledWith("ws-1", { status: "merged" });
  });

  it("opens on the filter from the overview link", () => {
    render(<IncidentsHome initialPendingFix />);
    expect(screen.getByRole("tab", { name: "Fix pending" })).toHaveAttribute("aria-selected", "true");
    expect(useIncidents).toHaveBeenLastCalledWith("ws-1", { pending_fix: true });
  });
});
