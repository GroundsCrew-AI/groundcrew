import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { connection } from "@/tests/discovery-fixtures";

import { AwsOperateDialog, isOperate } from "./aws-operate-dialog";

const mutate = vi.fn();
vi.mock("sonner", () => ({ toast: { success: vi.fn() } }));
vi.mock("@/lib/workspace", () => ({ useWorkspace: () => ({ workspace: { id: "ws-1" } }) }));
vi.mock("@/lib/discovery", () => ({
  downloadAwsTemplate: vi.fn(),
  useAwsSetOperate: () => ({ mutate, isPending: false, error: null, reset: vi.fn() }),
  useAwsTemplateUrl: () => ({ data: { url: "https://t.s3.us-east-2.amazonaws.com/aws-connect.yaml" }, isLoading: false }),
}));

describe("AwsOperateDialog", () => {
  beforeEach(() => mutate.mockReset());

  it("explains the stack update with this account's ARNs in its region and verifies the operate role", async () => {
    const conn = connection({ capabilities: ["observe"], region: "eu-west-1" });
    expect(isOperate(conn)).toBe(false);
    render(<AwsOperateDialog connection={conn} open onOpenChange={vi.fn()} />);
    expect(screen.getByText("Turn on emergency fixes")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "CloudFormation stacks" })).toHaveAttribute(
      "href",
      "https://eu-west-1.console.aws.amazon.com/cloudformation/home?region=eu-west-1#/stacks",
    );
    expect(screen.getByText("EnableOperate")).toBeInTheDocument();
    expect(screen.getByText("arn:aws:ecs:eu-west-1:881050425883:service/CLUSTER/*")).toBeInTheDocument();
    expect(screen.getByText("arn:aws:secretsmanager:eu-west-1:881050425883:secret:PREFIX/*")).toBeInTheDocument();
    expect(screen.getByText(/replace CLUSTER and PREFIX\)/)).toBeInTheDocument();
    expect(screen.getByText("OperatePassRoleArns")).toBeInTheDocument();
    // A pattern for the customer's own task roles, not a demo app's.
    expect(screen.getByText("arn:aws:iam::881050425883:role/YOUR-APP-*")).toBeInTheDocument();
    expect(screen.queryByText(/checkout|:us-east-2:/)).not.toBeInTheDocument();
    expect(screen.getByText(/only after a person approves each fix/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));
    expect(mutate).toHaveBeenCalledWith({ connectionId: conn.id, enabled: true }, expect.anything());
  });

  it("uses a REGION placeholder when the connection's region is not known yet", () => {
    render(<AwsOperateDialog connection={connection({ capabilities: ["observe"], region: null })} open onOpenChange={vi.fn()} />);
    expect(screen.getByText("arn:aws:ecs:REGION:881050425883:service/CLUSTER/*")).toBeInTheDocument();
    expect(screen.getByText(/replace CLUSTER, PREFIX, and REGION\)/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "CloudFormation stacks" })).toHaveAttribute(
      "href",
      "https://console.aws.amazon.com/cloudformation/home#/stacks",
    );
  });

  it("turns emergency fixes off", async () => {
    const conn = connection({ capabilities: ["observe", "operate"] });
    expect(isOperate(conn)).toBe(true);
    render(<AwsOperateDialog connection={conn} open onOpenChange={vi.fn()} />);
    expect(screen.queryByText("EnableOperate")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Turn off emergency fixes" }));
    expect(mutate).toHaveBeenCalledWith({ connectionId: conn.id, enabled: false }, expect.anything());
  });
});
