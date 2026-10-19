import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { connection } from "@/tests/discovery-fixtures";

import { deploySettingsBody, K8sDeploySettingsDialog } from "./k8s-deploy-settings-dialog";

const mutate = vi.fn();
const clear = vi.fn();
vi.mock("sonner", () => ({ toast: { success: vi.fn() } }));
vi.mock("@/lib/workspace", () => ({ useWorkspace: () => ({ workspace: { id: "ws-1" } }) }));
vi.mock("@/lib/discovery", () => ({
  useK8sSetDeploySettings: () => ({ mutate, isPending: false, error: null, reset: vi.fn() }),
  useK8sClearDeploySettings: () => ({ mutate: clear, isPending: false, error: null, reset: vi.fn() }),
}));

const SAVED = {
  registry: "ghcr.io/acme",
  username: "acme-bot",
  token_saved: true,
  pull_secret: true,
  ingress_class: "nginx",
  app_domain: "apps.acme.dev",
  tls: true,
  tls_cluster_issuer: "letsencrypt",
  public_url_base: null,
};

describe("K8sDeploySettingsDialog", () => {
  beforeEach(() => {
    mutate.mockReset();
    clear.mockReset();
  });

  it("saves a registry, credentials, ingress class, domain, and TLS", async () => {
    const conn = connection({ id: "conn-k8s", kind: "k8s", deploy_settings: null });
    render(<K8sDeploySettingsDialog connection={conn} open onOpenChange={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Use kind defaults" })).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Registry"), "ghcr.io/acme");
    await userEvent.type(screen.getByLabelText("Username"), "acme-bot");
    await userEvent.type(screen.getByLabelText("Token"), "ghp_secret");
    await userEvent.click(screen.getByRole("checkbox", { name: /imagePullSecret/ }));
    await userEvent.clear(screen.getByLabelText("Ingress class"));
    await userEvent.type(screen.getByLabelText("Ingress class"), "alb");
    await userEvent.type(screen.getByLabelText("App domain"), "apps.acme.dev");
    await userEvent.click(screen.getByRole("checkbox", { name: /HTTPS/ }));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(mutate).toHaveBeenCalledWith(
      {
        connectionId: "conn-k8s",
        body: {
          registry: "ghcr.io/acme",
          username: "acme-bot",
          token: "ghp_secret",
          pull_secret: true,
          ingress_class: "alb",
          app_domain: "apps.acme.dev",
          tls: true,
          tls_cluster_issuer: null,
          public_url_base: null,
        },
      },
      expect.anything(),
    );
  });

  it("shows a saved token as saved, keeps it, and can go back to the kind defaults", async () => {
    const conn = connection({ id: "conn-k8s", kind: "k8s", deploy_settings: SAVED });
    render(<K8sDeploySettingsDialog connection={conn} open onOpenChange={vi.fn()} />);
    expect(screen.getByLabelText("Registry")).toHaveValue("ghcr.io/acme");
    expect(screen.getByLabelText("Token")).toHaveValue("");
    expect(screen.getByText("Saved. Leave empty to keep it.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    const body = mutate.mock.calls[0]![0].body;
    expect(body.token).toBeNull();
    expect(body.tls_cluster_issuer).toBe("letsencrypt");
    await userEvent.click(screen.getByRole("button", { name: "Use kind defaults" }));
    expect(clear).toHaveBeenCalledWith("conn-k8s", expect.anything());
  });

  it("drops the token and pull secret without a username", () => {
    expect(
      deploySettingsBody({
        registry: " docker.io/acme ",
        username: "",
        token: "x",
        pullSecret: true,
        ingressClass: "",
        appDomain: "",
        tls: false,
      }),
    ).toEqual({
      registry: "docker.io/acme",
      username: null,
      token: null,
      pull_secret: false,
      ingress_class: "nginx",
      app_domain: null,
      tls: false,
      tls_cluster_issuer: null,
      public_url_base: null,
    });
  });
});
