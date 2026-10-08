import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AwsStart } from "@/lib/discovery";

import { AwsConnectDialog } from "./aws-connect-dialog";

const CONSOLE_URL =
  "https://us-east-2.console.aws.amazon.com/cloudformation/home?region=us-east-2#/stacks/quickcreate?stackName=groundcrew-read-only";

const state = vi.hoisted(() => ({
  start: { data: undefined as unknown, error: null as Error | null, isPending: false },
  complete: { data: undefined as unknown, error: null as Error | null, isPending: false },
  mutateComplete: vi.fn(),
  mutateStart: vi.fn(),
}));

vi.mock("@/lib/workspace", () => ({ useWorkspace: () => ({ workspace: { id: "ws-1" } }) }));
vi.mock("./run-discovery-button", () => ({ RunDiscoveryButton: () => null }));
vi.mock("@/lib/discovery", () => ({
  downloadAwsTemplate: vi.fn(),
  useAwsStart: () => ({ ...state.start, mutate: state.mutateStart, reset: vi.fn() }),
  useAwsComplete: () => ({ ...state.complete, mutate: state.mutateComplete, reset: vi.fn() }),
}));

function start(overrides: Partial<AwsStart> = {}): AwsStart {
  return {
    connection_id: "conn-1",
    external_id: "gc-abc123",
    principal_arn: "arn:aws:iam::881050425883:user/groundcrew-platform",
    region: "us-east-2",
    stack_name: "groundcrew-read-only",
    console_url: CONSOLE_URL,
    template_path: "/connections/aws/template",
    cli_command: "aws cloudformation create-stack --stack-name groundcrew-read-only",
    role_arn_command: "aws cloudformation describe-stacks",
    ...overrides,
  };
}

function fakeTab() {
  return { location: { href: "" }, close: vi.fn(), document: { body: { textContent: "" } } } as unknown as Window;
}

describe("AwsConnectDialog", () => {
  beforeEach(() => {
    state.start = { data: undefined, error: null, isPending: false };
    state.complete = { data: undefined, error: null, isPending: false };
    state.mutateComplete.mockReset();
    state.mutateStart.mockReset();
  });

  it("sends a new tab to CloudFormation once the link arrives, and closes it on an error", () => {
    const ok = { current: fakeTab() };
    const okTab = ok.current;
    render(<AwsConnectDialog open onOpenChange={vi.fn()} tab={ok} />);
    const [workspaceId, callbacks] = state.mutateStart.mock.calls[0];
    expect(workspaceId).toBe("ws-1");
    callbacks.onSuccess(start());
    expect(okTab.location.href).toBe(CONSOLE_URL);

    const failed = { current: fakeTab() };
    const failedTab = failed.current;
    state.mutateStart.mockReset();
    render(<AwsConnectDialog open onOpenChange={vi.fn()} tab={failed} />);
    state.mutateStart.mock.calls[0][1].onError(new Error("Workspace not found."));
    expect(failedTab.close).toHaveBeenCalled();
    expect(failedTab.document.body.textContent).toContain("Workspace not found.");
  });

  it("on reopen, points the new tab at the link it already has, and shows three steps", () => {
    state.start.data = start();
    const tab = { current: fakeTab() };
    const waiting = tab.current;
    render(<AwsConnectDialog open onOpenChange={vi.fn()} tab={tab} />);
    expect(waiting.location.href).toBe(CONSOLE_URL);
    expect(tab.current).toBeNull();
    expect(screen.getByRole("link", { name: /Open it again/ })).toHaveAttribute("href", CONSOLE_URL);
    expect(screen.getAllByRole("listitem").length).toBeGreaterThanOrEqual(3);
    expect(screen.getByText(/CREATE_COMPLETE/)).toBeInTheDocument();
  });

  it("closes the tab and offers the CLI when one-click setup is not configured", () => {
    state.start.data = start({ console_url: null });
    const tab = { current: fakeTab() };
    const waiting = tab.current;
    render(<AwsConnectDialog open onOpenChange={vi.fn()} tab={tab} />);
    expect(waiting.close).toHaveBeenCalled();
    expect(screen.getByText(/One-click setup is not available/)).toBeInTheDocument();
    expect(screen.getByText(/aws cloudformation create-stack/)).toBeVisible();
  });

  it("verifies the pasted RoleArn and shows why it failed", async () => {
    state.start.data = start();
    state.complete.error = new Error(
      "Groundcrew could not assume the role. Usually the stack was created with a different ExternalId.",
    );
    render(<AwsConnectDialog open onOpenChange={vi.fn()} />);
    await userEvent.type(
      screen.getByLabelText("RoleArn"),
      "arn:aws:iam::881050425883:role/groundcrew-read-only",
    );
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));
    expect(state.mutateComplete).toHaveBeenCalledWith(
      { connection_id: "conn-1", role_arn: "arn:aws:iam::881050425883:role/groundcrew-read-only" },
      expect.anything(),
    );
    expect(screen.getByRole("alert")).toHaveTextContent("different ExternalId");
  });
});
