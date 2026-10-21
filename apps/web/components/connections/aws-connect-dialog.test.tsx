import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AwsStart } from "@/lib/discovery";

import { AwsConnectDialog } from "./aws-connect-dialog";

const state = vi.hoisted(() => ({
  start: { data: undefined as unknown, error: null as Error | null, isPending: false },
  complete: { data: undefined as unknown, error: null as Error | null, isPending: false },
  mutateComplete: vi.fn(),
  mutateStart: vi.fn(),
}));

vi.mock("@/lib/workspace", () => ({ useWorkspace: () => ({ workspace: { id: "ws-1" } }) }));
vi.mock("./run-discovery-button", () => ({ RunDiscoveryButton: () => null }));
vi.mock("@/lib/discovery", () => ({
  useAwsStart: () => ({ ...state.start, mutate: state.mutateStart, reset: vi.fn() }),
  useAwsComplete: () => ({ ...state.complete, mutate: state.mutateComplete, reset: vi.fn() }),
}));

const PRINCIPAL = "arn:aws:iam::881050425883:role/groundcrew-platform";

function start(overrides: Partial<AwsStart> = {}): AwsStart {
  return {
    connection_id: "conn-1",
    tier: "observe",
    external_id: "gc-abc123",
    principal_arn: PRINCIPAL,
    region: "us-east-2",
    stack_name: "groundcrew-read-only",
    console_url: null,
    template_path: "/connections/aws/template",
    template_download_url: "https://groundkrew.com/aws-connect.yaml",
    upload_console_url: "https://us-east-2.console.aws.amazon.com/cloudformation/home?region=us-east-2#/stacks/create",
    parameters: [
      { key: "GroundcrewPrincipalArn", value: PRINCIPAL },
      { key: "ExternalId", value: "gc-abc123" },
      { key: "Tier", value: "observe" },
    ],
    cli_command: "aws cloudformation create-stack --stack-name groundcrew-read-only",
    role_arn_command: "aws cloudformation describe-stacks",
    ...overrides,
  };
}

describe("AwsConnectDialog", () => {
  beforeEach(() => {
    state.start = { data: undefined, error: null, isPending: false };
    state.complete = { data: undefined, error: null, isPending: false };
    state.mutateComplete.mockReset();
    state.mutateStart.mockReset();
  });

  it("asks the API for this workspace's parameters once it opens", () => {
    render(<AwsConnectDialog open onOpenChange={vi.fn()} tier="deploy" />);
    expect(state.mutateStart).toHaveBeenCalledWith({ workspaceId: "ws-1", tier: "deploy" });
    expect(screen.getByText(/Preparing this workspace/)).toBeInTheDocument();
  });

  it("walks through the template upload with every value to copy", () => {
    state.start.data = start();
    render(<AwsConnectDialog open onOpenChange={vi.fn()} />);
    expect(screen.getByRole("link", { name: /aws-connect.yaml/ })).toHaveAttribute(
      "href",
      "https://groundkrew.com/aws-connect.yaml",
    );
    expect(screen.getByRole("link", { name: /Open CloudFormation/ })).toHaveAttribute(
      "href",
      expect.stringContaining("#/stacks/create"),
    );
    expect(screen.getByText(/Upload a template file/)).toBeInTheDocument();
    for (const label of ["Stack name", "GroundcrewPrincipalArn", "ExternalId", "Tier"]) {
      expect(screen.getByRole("button", { name: `Copy ${label}` })).toBeInTheDocument();
    }
    expect(screen.getByText("gc-abc123")).toBeInTheDocument();
    expect(state.mutateStart).not.toHaveBeenCalled();
  });

  it("offers the CLI, and the filled-in link only when the server has one", async () => {
    state.start.data = start();
    const { unmount } = render(<AwsConnectDialog open onOpenChange={vi.fn()} />);
    await userEvent.click(screen.getByText("With the AWS CLI instead"));
    expect(screen.getByRole("button", { name: "Copy Create the stack" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /filled-in CloudFormation link/ })).not.toBeInTheDocument();
    unmount();
    state.start.data = start({ console_url: "https://us-east-2.console.aws.amazon.com/quickcreate" });
    render(<AwsConnectDialog open onOpenChange={vi.fn()} />);
    await userEvent.click(screen.getByText("With the AWS CLI instead"));
    expect(screen.getByRole("link", { name: /filled-in CloudFormation link/ })).toBeInTheDocument();
  });

  it("verifies the pasted RoleArn", async () => {
    state.start.data = start();
    render(<AwsConnectDialog open onOpenChange={vi.fn()} />);
    await userEvent.type(screen.getByLabelText("RoleArn"), " arn:aws:iam::123456789012:role/groundcrew-read-only ");
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));
    expect(state.mutateComplete.mock.calls[0][0]).toEqual({
      connection_id: "conn-1",
      role_arn: "arn:aws:iam::123456789012:role/groundcrew-read-only",
    });
  });
});
