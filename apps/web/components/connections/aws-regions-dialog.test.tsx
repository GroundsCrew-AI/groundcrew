import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { connection } from "@/tests/discovery-fixtures";

import { AwsRegionsDialog } from "./aws-regions-dialog";

const mutate = vi.fn();
vi.mock("sonner", () => ({ toast: { success: vi.fn() } }));
vi.mock("@/lib/workspace", () => ({ useWorkspace: () => ({ workspace: { id: "ws-1" } }) }));
vi.mock("@/lib/discovery", () => ({
  useAwsSetRegions: () => ({ mutate, isPending: false, error: null, reset: vi.fn() }),
}));

describe("AwsRegionsDialog", () => {
  beforeEach(() => mutate.mockReset());

  it("starts from the connection's regions and saves the new choice", async () => {
    const conn = connection({ regions: ["us-east-2"] });
    render(<AwsRegionsDialog connection={conn} open onOpenChange={vi.fn()} />);
    expect(screen.getByRole("checkbox", { name: /us-east-2/ })).toBeChecked();
    await userEvent.click(screen.getByRole("checkbox", { name: /eu-west-1/ }));
    await userEvent.click(screen.getByRole("button", { name: "Save regions" }));
    expect(mutate).toHaveBeenCalledWith(
      { connectionId: conn.id, regions: ["us-east-2", "eu-west-1"] },
      expect.anything(),
    );
  });

  it("needs at least one region", async () => {
    render(<AwsRegionsDialog connection={connection({ regions: ["us-east-2"] })} open onOpenChange={vi.fn()} />);
    await userEvent.click(screen.getByRole("checkbox", { name: /us-east-2/ }));
    expect(screen.getByRole("button", { name: "Save regions" })).toBeDisabled();
  });
});
