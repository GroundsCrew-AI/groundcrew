import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { connection } from "@/tests/discovery-fixtures";

import {
  K8S_CONFIGMAPS_RBAC,
  K8sConfigMapsDialog,
} from "./k8s-configmaps-dialog";

const mutate = vi.fn();
vi.mock("sonner", () => ({ toast: { success: vi.fn() } }));
vi.mock("@/lib/workspace", () => ({
  useWorkspace: () => ({ workspace: { id: "ws-1" } }),
}));
vi.mock("@/lib/discovery", () => ({
  useK8sSetConfigMapChanges: () => ({
    mutate,
    isPending: false,
    error: null,
    reset: vi.fn(),
  }),
}));

describe("K8sConfigMapsDialog", () => {
  beforeEach(() => mutate.mockReset());

  it("explains the right grants read access to contents and verifies it", async () => {
    const conn = connection({ kind: "k8s", configmap_changes: false });
    render(
      <K8sConfigMapsDialog connection={conn} open onOpenChange={vi.fn()} />,
    );
    for (const command of K8S_CONFIGMAPS_RBAC)
      expect(screen.getByText(command)).toBeInTheDocument();
    expect(
      screen.getByText(/grants read access to ConfigMap contents/),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));
    expect(mutate).toHaveBeenCalledWith(
      { connectionId: conn.id, enabled: true },
      expect.anything(),
    );
  });

  it("turns detection off", async () => {
    const conn = connection({ kind: "k8s", configmap_changes: true });
    render(
      <K8sConfigMapsDialog connection={conn} open onOpenChange={vi.fn()} />,
    );
    expect(screen.queryByText(K8S_CONFIGMAPS_RBAC[0])).not.toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: "Stop detecting" }),
    );
    expect(mutate).toHaveBeenCalledWith(
      { connectionId: conn.id, enabled: false },
      expect.anything(),
    );
  });
});
