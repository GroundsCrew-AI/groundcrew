import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { connection } from "@/tests/discovery-fixtures";

import { AwsTierDialog } from "./aws-tier-dialog";

const mutate = vi.fn();
vi.mock("sonner", () => ({ toast: { success: vi.fn() } }));
vi.mock("@/lib/workspace", () => ({ useWorkspace: () => ({ workspace: { id: "ws-1" } }) }));
vi.mock("@/lib/discovery", () => ({
  useAwsSetTier: () => ({ mutate, isPending: false, error: null, reset: vi.fn() }),
}));

describe("AwsTierDialog", () => {
  beforeEach(() => mutate.mockReset());

  it("explains the stack update and verifies the deploy role", async () => {
    const conn = connection({ tier: "observe" });
    render(<AwsTierDialog connection={conn} open onOpenChange={vi.fn()} />);
    expect(screen.getByText("Turn on deploys")).toBeInTheDocument();
    expect(screen.getByText(/Replace existing template/)).toBeInTheDocument();
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
