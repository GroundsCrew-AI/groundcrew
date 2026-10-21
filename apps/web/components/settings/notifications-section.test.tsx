import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { WorkspaceDetail } from "@/lib/workspace-admin";

import { NotificationsSection } from "./notifications-section";

// jsdom has no ResizeObserver; radix Switch measures itself with it.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
};

const update = vi.fn();
const test = { mutate: vi.fn(), isPending: false, error: null as Error | null, data: undefined as unknown };
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("@/lib/workspace-admin", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/workspace-admin")>()),
  useUpdateWorkspace: () => ({ mutateAsync: update, isPending: false }),
  useTestNotifications: () => test,
}));

const HOOK = "https://hooks.slack.com/services/T000/B000/abcdef";

function workspace(patch: Partial<WorkspaceDetail> = {}): WorkspaceDetail {
  return {
    id: "ws-1",
    name: "Acme",
    role: "owner",
    created_at: "2026-10-01T00:00:00Z",
    slack_saved: false,
    settings: { emergency_approvers: "owners", incident_watch: true, daily_usd_cap: null },
    ...patch,
  };
}

describe("NotificationsSection", () => {
  beforeEach(() => {
    update.mockReset().mockResolvedValue(workspace());
    test.mutate.mockReset();
    test.data = undefined;
  });

  it("saves the webhook write-only and checks it first", async () => {
    render(<NotificationsSection workspace={workspace()} canEdit />);
    const field = screen.getByLabelText("Slack incoming webhook");
    expect(field).toHaveAttribute("type", "password");
    await userEvent.type(field, "https://example.com/hook");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByRole("alert")).toHaveTextContent("not a Slack incoming webhook");
    expect(update).not.toHaveBeenCalled();

    await userEvent.clear(field);
    await userEvent.type(field, HOOK);
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(update).toHaveBeenCalledWith({ slack_webhook_url: HOOK });
  });

  it("shows a saved webhook as Saved and clears it", async () => {
    render(<NotificationsSection workspace={workspace({ slack_saved: true })} canEdit />);
    expect(screen.getByText("Saved")).toBeInTheDocument();
    expect(screen.queryByLabelText("Slack incoming webhook")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(update).toHaveBeenCalledWith({ slack_webhook_url: null });
  });

  it("saves the audience and event toggles with the other settings", async () => {
    const ws = workspace();
    render(<NotificationsSection workspace={ws} canEdit />);
    expect(screen.getByRole("radio", { name: "Owners only" })).toBeChecked();
    await userEvent.click(screen.getByRole("radio", { name: "Every member" }));
    await userEvent.click(screen.getByRole("switch", { name: "Incident resolved" }));
    await userEvent.click(screen.getByRole("button", { name: "Save notifications" }));
    expect(update).toHaveBeenCalledWith({
      settings: {
        ...ws.settings,
        notifications: {
          email: "members",
          incident_opened: true,
          approval_needed: true,
          incident_resolved: false,
          incident_failed: true,
        },
      },
    });
  });

  it("sends a test and shows each channel's result", async () => {
    test.data = {
      results: [
        { channel: "slack", ok: false, detail: "Slack refused the message: HTTP 404 no_service" },
        { channel: "email", ok: true, detail: "Sent to 2 people." },
      ],
    };
    render(<NotificationsSection workspace={workspace({ slack_saved: true })} canEdit />);
    await userEvent.click(screen.getByRole("button", { name: "Send test" }));
    expect(test.mutate).toHaveBeenCalled();
    const results = screen.getByRole("status");
    expect(within(results).getByText(/HTTP 404 no_service/)).toBeInTheDocument();
    expect(within(results).getByText(/Sent to 2 people/)).toBeInTheDocument();
  });

  it("is read-only for approvers and viewers", () => {
    render(<NotificationsSection workspace={workspace({ role: "approver", slack_saved: true })} canEdit={false} />);
    expect(screen.getByText("Saved")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Owners only" })).toBeDisabled();
    expect(screen.getByRole("switch", { name: "Approval needed" })).toBeDisabled();
  });
});
