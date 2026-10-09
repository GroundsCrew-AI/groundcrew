import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { connection } from "@/tests/discovery-fixtures";

import { AwsTierDialog } from "./aws-tier-dialog";

const mutate = vi.fn();
const template = vi.hoisted(() => ({ data: { url: null as string | null, template_path: "/connections/aws/template" } }));
vi.mock("sonner", () => ({ toast: { success: vi.fn() } }));
vi.mock("@/lib/workspace", () => ({ useWorkspace: () => ({ workspace: { id: "ws-1" } }) }));
vi.mock("@/lib/discovery", () => ({
  downloadAwsTemplate: vi.fn(),
  useAwsSetTier: () => ({ mutate, isPending: false, error: null, reset: vi.fn() }),
  useAwsTemplateUrl: () => ({ ...template, isLoading: false }),
}));

const URL = "https://groundcrew-templates-881050425883.s3.us-east-2.amazonaws.com/aws-connect.yaml";

describe("AwsTierDialog", () => {
  beforeEach(() => {
    mutate.mockReset();
    template.data.url = URL;
  });

  it("explains the stack update and verifies the deploy role", async () => {
    const conn = connection({ tier: "observe" });
    render(<AwsTierDialog connection={conn} open onOpenChange={vi.fn()} />);
    expect(screen.getByText("Turn on deploys")).toBeInTheDocument();
    expect(screen.getByText(/Replace existing template/)).toBeInTheDocument();
    expect(screen.getByText("Template URL")).toBeInTheDocument();
    expect(screen.getByText(URL)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /copy/i })).toBeInTheDocument();
    expect(screen.queryByText(/Other ways to connect/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));
    expect(mutate).toHaveBeenCalledWith({ connectionId: conn.id, tier: "deploy" }, expect.anything());
  });

  it("turns deploys off", async () => {
    const conn = connection({ tier: "deploy" });
    render(<AwsTierDialog connection={conn} open onOpenChange={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Turn off deploys" }));
    expect(mutate).toHaveBeenCalledWith({ connectionId: conn.id, tier: "observe" }, expect.anything());
  });
});

describe("AwsTierDialog without a template URL", () => {
  it("offers the template file to upload instead", () => {
    template.data.url = null;
    render(<AwsTierDialog connection={connection({ tier: "observe" })} open onOpenChange={vi.fn()} />);
    expect(screen.getByText(/Upload a template file/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /aws-connect.yaml/ })).toBeInTheDocument();
    expect(screen.queryByText("Template URL")).not.toBeInTheDocument();
  });
});
