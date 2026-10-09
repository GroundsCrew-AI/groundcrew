import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { connection } from "@/tests/discovery-fixtures";

import { ConnectionRow, lastDiscoveryText } from "./connection-row";

vi.mock("@/lib/discovery", async (original) => ({
  ...(await original<typeof import("@/lib/discovery")>()),
  useDeleteConnection: () => ({ mutate: vi.fn(), isPending: false }),
}));

describe("ConnectionRow", () => {
  it("shows status, role, and the last discovery", () => {
    const conn = connection({
      last_discovery: { at: new Date().toISOString(), run_id: "run-1", status: "succeeded", resources: 21, truncated: false, notes: [] },
    });
    render(<ConnectionRow connection={conn} onRefresh={vi.fn()} refreshing={false} />);
    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect(screen.getByText(conn.role_arn!)).toBeInTheDocument();
    expect(screen.getByText(/Discovered just now, 21 resources/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View run" })).toHaveAttribute("href", "/runs/run-1");
  });

  it("shows an AWS connection's tier and offers to switch it", () => {
    const { rerender } = render(<ConnectionRow connection={connection()} onRefresh={vi.fn()} refreshing={false} />);
    expect(screen.getByText("Observe")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enable deploys" })).toBeInTheDocument();
    rerender(<ConnectionRow connection={connection({ tier: "deploy" })} onRefresh={vi.fn()} refreshing={false} />);
    expect(screen.getByText("Deploy")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Turn off deploys" })).toBeInTheDocument();
  });

  it("refreshes a connected connection", async () => {
    const onRefresh = vi.fn();
    const conn = connection();
    render(<ConnectionRow connection={conn} onRefresh={onRefresh} refreshing={false} />);
    await userEvent.click(screen.getByRole("button", { name: /refresh/i }));
    expect(onRefresh).toHaveBeenCalledWith(conn);
  });

  it("offers to finish a pending AWS connection", async () => {
    const onResume = vi.fn();
    render(
      <ConnectionRow
        connection={connection({ status: "pending", role_arn: null, error: "This role can be assumed without the ExternalId." })}
        onRefresh={vi.fn()}
        refreshing={false}
        onResume={onResume}
      />,
    );
    expect(screen.getByText("Not finished")).toBeInTheDocument();
    expect(screen.getByText(/without the ExternalId/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Finish connecting" }));
    expect(onResume).toHaveBeenCalled();
  });

  it("marks previews and cannot refresh them", () => {
    render(<ConnectionRow connection={connection({ kind: "azure", preview: true, status: "pending" })} onRefresh={vi.fn()} refreshing={false} />);
    expect(screen.getByText("Preview")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /refresh/i })).toBeDisabled();
  });

  it("explains failed discoveries", () => {
    const text = lastDiscoveryText(
      connection({ last_discovery: { at: new Date().toISOString(), status: "failed", error: "AccessDenied", truncated: false, notes: [] } }),
    );
    expect(text).toBe("Discovery failed just now: AccessDenied");
  });
});
