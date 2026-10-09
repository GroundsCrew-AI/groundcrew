import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { KubernetesConnectDialog } from "./token-connect-dialogs";

const mutate = vi.fn();
const success = vi.fn();

vi.mock("sonner", () => ({ toast: { success: (...args: unknown[]) => success(...args) } }));
vi.mock("@/lib/workspace", () => ({ useWorkspace: () => ({ workspace: { id: "ws-1" } }) }));
vi.mock("@/lib/discovery", () => ({
  useConnectAzure: () => ({}),
  useConnectVercel: () => ({}),
  useConnectKubernetes: () => ({ mutate, isPending: false, error: null, reset: vi.fn() }),
}));

const KUBECONFIG = "apiVersion: v1\nclusters: [{name: groundcrew}]\n";

describe("KubernetesConnectDialog", () => {
  beforeEach(() => {
    mutate.mockReset();
    success.mockReset();
  });

  it("loads an uploaded kubeconfig, verifies it, and reports the namespaces", async () => {
    const onOpenChange = vi.fn();
    render(<KubernetesConnectDialog open onOpenChange={onOpenChange} />);
    expect(screen.queryByText(/Preview/)).not.toBeInTheDocument();
    const file = new File([KUBECONFIG], "groundcrew-deployer.kubeconfig", { type: "text/plain" });
    await userEvent.upload(screen.getByLabelText("Kubeconfig file"), file);
    await waitFor(() => expect(screen.getByLabelText("Kubeconfig")).toHaveValue(KUBECONFIG));
    expect(screen.getByText("Loaded groundcrew-deployer.kubeconfig")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Verify and connect" }));
    expect(mutate).toHaveBeenCalledWith(
      { workspace_id: "ws-1", name: null, kubeconfig: KUBECONFIG },
      expect.anything(),
    );
    mutate.mock.calls[0]![1].onSuccess({ account: "groundcrew", namespaces: 7 });
    expect(success).toHaveBeenCalledWith("Connected to groundcrew: 7 namespaces");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});
