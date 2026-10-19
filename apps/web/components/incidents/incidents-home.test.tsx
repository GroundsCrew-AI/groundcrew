import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { IncidentsHome } from "@/app/(app)/incidents/incidents-home";
import { incidentSummary } from "@/tests/incident-fixtures";

const useIncidents = vi.fn();
vi.mock("next/navigation", () => ({
  usePathname: () => "/incidents",
  // Read the URL the component wrote with the History API, as the App Router does.
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
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

// Radix Select needs these in jsdom.
beforeAll(() => {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.releasePointerCapture = () => {};
  Element.prototype.scrollIntoView = () => {};
});
afterAll(() => window.history.replaceState(null, "", "/"));

function page(items = [incidentSummary()], hasNextPage = false) {
  useIncidents.mockReturnValue({ data: { pages: [{ items }] }, isLoading: false, error: null, hasNextPage });
}

function at(url: string) {
  window.history.replaceState(null, "", url);
}

describe("IncidentsHome", () => {
  beforeEach(() => {
    useIncidents.mockReset();
    page();
    at("/incidents");
  });

  it("puts a status tab in the URL and sends it to the API", async () => {
    const push = vi.spyOn(window.history, "pushState");
    const { rerender } = render(<IncidentsHome />);
    expect(useIncidents).toHaveBeenLastCalledWith("ws-1", {});
    await userEvent.click(screen.getByRole("tab", { name: "Fix pending" }));
    expect(push).toHaveBeenLastCalledWith(null, "", "/incidents?pending_fix=true");
    rerender(<IncidentsHome />);
    expect(useIncidents).toHaveBeenLastCalledWith("ws-1", { pending_fix: true });
    expect(screen.getByRole("tab", { name: "Fix pending" })).toHaveAttribute("aria-selected", "true");
    push.mockRestore();
  });

  it("opens on the filters in the URL (from the overview link, a reload, or back/forward)", () => {
    at("/incidents?status=merged&severity=high&window=7d");
    const { rerender } = render(<IncidentsHome />);
    expect(useIncidents).toHaveBeenLastCalledWith("ws-1", { status: "merged", severity: "high", window: "7d" });
    expect(screen.getByRole("tab", { name: "Merged" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("combobox", { name: "Severity" })).toHaveTextContent("High");
    expect(screen.getByRole("combobox", { name: "Opened" })).toHaveTextContent("Last 7 days");

    // Back to an earlier entry: the list follows the URL.
    at("/incidents?pending_fix=true&q=worker");
    rerender(<IncidentsHome />);
    expect(useIncidents).toHaveBeenLastCalledWith("ws-1", { pending_fix: true });
    expect(screen.getByRole("searchbox", { name: /Search incidents/ })).toHaveValue("worker");
  });

  it("filters by severity from the menu, keeping the other filters", async () => {
    at("/incidents?status=resolved");
    const push = vi.spyOn(window.history, "pushState");
    render(<IncidentsHome />);
    await userEvent.click(screen.getByRole("combobox", { name: "Severity" }));
    await userEvent.click(screen.getByRole("option", { name: "Critical" }));
    expect(push).toHaveBeenLastCalledWith(null, "", "/incidents?status=resolved&severity=critical");
    push.mockRestore();
  });

  it("searches the loaded incidents by title and resource, in the URL without new history", async () => {
    page([
      incidentSummary({ id: "a", title: "api health checks failing", affected_resources: [] }),
      incidentSummary({ id: "b", title: "Tasks crashing" }),
    ]);
    const replace = vi.spyOn(window.history, "replaceState");
    render(<IncidentsHome />);
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    await userEvent.type(screen.getByRole("searchbox", { name: /Search incidents/ }), "worker");
    expect(replace).toHaveBeenLastCalledWith(null, "", "/incidents?q=worker");
    const rows = screen.getAllByRole("listitem");
    expect(rows).toHaveLength(1);
    expect(within(rows[0]!).getByTitle("Tasks crashing")).toBeInTheDocument();
    // The API is not asked again: search is client-side.
    expect(useIncidents).toHaveBeenLastCalledWith("ws-1", {});
    replace.mockRestore();
  });

  it("says what matched nothing, and offers to clear", async () => {
    page([]);
    at("/incidents?severity=critical&window=24h");
    const push = vi.spyOn(window.history, "pushState");
    render(<IncidentsHome />);
    expect(screen.getByText("No critical severity incidents in the last 24 hours")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(push).toHaveBeenLastCalledWith(null, "", "/incidents");
    push.mockRestore();
  });

  it("explains a search with no match over the loaded page", () => {
    page([incidentSummary()], true);
    at("/incidents?q=database");
    render(<IncidentsHome />);
    expect(screen.getByText('No incidents match "database"')).toBeInTheDocument();
    expect(screen.getByText(/Load older incidents to search further back/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load older incidents" })).toBeInTheDocument();
  });

  it("keeps the plain empty state without filters", () => {
    page([]);
    render(<IncidentsHome />);
    expect(screen.getByText("No incidents")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Clear/ })).not.toBeInTheDocument();
  });
});
