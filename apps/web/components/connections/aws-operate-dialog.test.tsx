import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AwsOperateValues } from "@/lib/discovery";
import { connection } from "@/tests/discovery-fixtures";

import { AwsOperateDialog, isOperate } from "./aws-operate-dialog";

const mutate = vi.fn();
let values: AwsOperateValues | undefined;
vi.mock("sonner", () => ({ toast: { success: vi.fn() } }));
vi.mock("@/lib/workspace", () => ({ useWorkspace: () => ({ workspace: { id: "ws-1" } }) }));
vi.mock("@/lib/discovery", () => ({
  downloadAwsTemplate: vi.fn(),
  useAwsSetOperate: () => ({ mutate, isPending: false, error: null, reset: vi.fn() }),
  useAwsTemplateUrl: () => ({ data: { url: "https://t.s3.us-east-2.amazonaws.com/aws-connect.yaml" }, isLoading: false }),
  useAwsOperateValues: () => ({ data: values, isLoading: false }),
}));

const EMPTY: AwsOperateValues = { services: [], pass_roles: [], functions: [], secrets: [], instances: [], discovered: false };

describe("AwsOperateDialog", () => {
  beforeEach(() => {
    mutate.mockReset();
    values = EMPTY;
  });

  it("fills the parameters from discovery, so there is nothing to replace", async () => {
    values = {
      ...EMPTY,
      services: ["arn:aws:ecs:eu-west-1:881050425883:service/shop/*"],
      pass_roles: ["arn:aws:iam::881050425883:role/shop-api-task-execution"],
      functions: ["arn:aws:lambda:eu-west-1:881050425883:function:shop-url"],
      discovered: true,
    };
    const conn = connection({ capabilities: ["observe"], region: "eu-west-1" });
    expect(isOperate(conn)).toBe(false);
    render(<AwsOperateDialog connection={conn} open onOpenChange={vi.fn()} />);
    expect(screen.getByText("Turn on emergency fixes")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /CloudFormation stacks/ })).toHaveAttribute(
      "href",
      "https://eu-west-1.console.aws.amazon.com/cloudformation/home?region=eu-west-1#/stacks",
    );
    expect(screen.getByText("arn:aws:ecs:eu-west-1:881050425883:service/shop/*")).toBeInTheDocument();
    expect(screen.getByText("every service in shop")).toBeInTheDocument();
    expect(screen.getByText("arn:aws:iam::881050425883:role/shop-api-task-execution")).toBeInTheDocument();
    expect(screen.getByText("arn:aws:lambda:eu-west-1:881050425883:function:shop-url")).toBeInTheDocument();
    expect(screen.getByText("OperateSecretArns, OperateInstanceArns")).toBeInTheDocument();
    expect(screen.queryByText(/Replace .* before you paste it/)).not.toBeInTheDocument();
    expect(screen.queryByText(/CLUSTER|PREFIX|YOUR-APP/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));
    expect(mutate).toHaveBeenCalledWith({ connectionId: conn.id, enabled: true }, expect.anything());
  });

  it("calls out every word to replace when discovery found nothing", () => {
    render(<AwsOperateDialog connection={connection({ capabilities: ["observe"], region: null })} open onOpenChange={vi.fn()} />);
    expect(screen.getByText(/Groundcrew has not found services in this account yet/)).toBeInTheDocument();
    expect(screen.getByText("arn:aws:ecs:REGION:881050425883:service/CLUSTER/*")).toBeInTheDocument();
    expect(screen.getByText("Replace CLUSTER and REGION with your own value before you paste it.")).toBeInTheDocument();
    expect(screen.getByText("Replace YOUR-APP-* with your own value before you paste it.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /CloudFormation stacks/ })).toHaveAttribute(
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
