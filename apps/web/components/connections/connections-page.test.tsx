import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ConnectionsPage from "@/app/(app)/connections/page";
import { connection } from "@/tests/discovery-fixtures";

const role = vi.hoisted(() => ({ value: "owner" as "owner" | "member" }));

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/lib/workspace", () => ({
  useWorkspace: () => ({ workspace: { id: "ws-1", name: "Acme", role: role.value }, error: null, refetch: vi.fn() }),
}));
vi.mock("@/lib/discovery", async (original) => ({
  ...(await original<typeof import("@/lib/discovery")>()),
  useConnections: () => ({ data: [connection()], isLoading: false, error: null }),
  useRefreshConnection: () => ({ mutate: vi.fn(), isPending: false }),
  useDeleteConnection: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/components/shell/page-header", () => ({
  PageHeader: () => null,
  PageBody: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
vi.mock("@/components/connections/aws-connect-dialog", () => ({ AwsConnectDialog: () => null, openPendingTab: () => null }));
vi.mock("@/components/connections/github-connect-dialog", () => ({ GitHubConnectDialog: () => null }));
vi.mock("@/components/connections/token-connect-dialogs", () => ({
  AzureConnectDialog: () => null,
  KubernetesConnectDialog: () => null,
  VercelConnectDialog: () => null,
}));

const REASON = "Only workspace owners can add, change, or remove connections.";

describe("ConnectionsPage", () => {
  beforeEach(() => {
    role.value = "owner";
  });

  it("lets owners add connections", () => {
    render(<ConnectionsPage />);
    expect(screen.queryByText(new RegExp(REASON))).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect AWS" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Pick repositories" })).toBeEnabled();
  });

  it("shows members why connection actions are disabled", () => {
    role.value = "member";
    render(<ConnectionsPage />);
    expect(screen.getByText(new RegExp(REASON))).toBeInTheDocument();
    for (const name of ["Connect AWS", "Pick repositories", "Add token", "Upload kubeconfig", "Enable deploys"]) {
      const button = screen.getByRole("button", { name });
      expect(button).toBeDisabled();
      expect(button).toHaveAccessibleDescription(new RegExp(REASON));
    }
    expect(screen.getByRole("radio", { name: "Deploy" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /refresh/i })).toBeEnabled();
  });
});
