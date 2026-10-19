import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { connection } from "@/tests/discovery-fixtures";

import { K8sOperateDialog } from "./k8s-operate-dialog";

const mutate = vi.fn();
vi.mock("sonner", () => ({ toast: { success: vi.fn() } }));
vi.mock("@/lib/workspace", () => ({ useWorkspace: () => ({ workspace: { id: "ws-1" } }) }));
vi.mock("@/lib/discovery", () => ({
  useK8sSetOperate: () => ({ mutate, isPending: false, error: null, reset: vi.fn() }),
}));

describe("K8sOperateDialog", () => {
  beforeEach(() => mutate.mockReset());

  it("says which rights to grant and verifies them", async () => {
    const conn = connection({ kind: "k8s", capabilities: ["observe"] });
    render(<K8sOperateDialog connection={conn} open onOpenChange={vi.fn()} />);
    expect(screen.getByText("kubectl apply -f infra/k8s-operate-rbac.yaml")).toBeInTheDocument();
    expect(screen.getByText(/kubectl rollout undo/)).toBeInTheDocument();
    expect(screen.getByText(/only after a person approves each fix/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));
    expect(mutate).toHaveBeenCalledWith({ connectionId: conn.id, enabled: true }, expect.anything());
  });

  it("turns emergency fixes off", async () => {
    const conn = connection({ kind: "k8s", capabilities: ["observe", "operate"] });
    render(<K8sOperateDialog connection={conn} open onOpenChange={vi.fn()} />);
    expect(screen.queryByText(/k8s-operate-rbac/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Turn off emergency fixes" }));
    expect(mutate).toHaveBeenCalledWith({ connectionId: conn.id, enabled: false }, expect.anything());
  });
});
