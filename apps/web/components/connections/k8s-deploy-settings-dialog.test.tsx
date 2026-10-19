import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { connection } from "@/tests/discovery-fixtures";

import {
  deploySettingsBody,
  ecrRegistry,
  K8sDeploySettingsDialog,
} from "./k8s-deploy-settings-dialog";

const mutate = vi.fn();
const clear = vi.fn();
const AWS_DEPLOY = connection({
  id: "conn-aws",
  kind: "aws",
  name: "AWS 123456789012",
  account: "123456789012",
  region: "us-east-2",
  capabilities: ["observe", "deploy"],
});
const AWS_OBSERVE = connection({
  id: "conn-aws-2",
  kind: "aws",
  name: "AWS observe",
  capabilities: ["observe"],
});
vi.mock("sonner", () => ({ toast: { success: vi.fn() } }));
vi.mock("@/lib/workspace", () => ({
  useWorkspace: () => ({ workspace: { id: "ws-1" } }),
}));
vi.mock("@/lib/discovery", () => ({
  useConnections: () => ({ data: [AWS_DEPLOY, AWS_OBSERVE] }),
  useK8sSetDeploySettings: () => ({
    mutate,
    isPending: false,
    error: null,
    reset: vi.fn(),
  }),
  useK8sClearDeploySettings: () => ({
    mutate: clear,
    isPending: false,
    error: null,
    reset: vi.fn(),
  }),
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
  registry_auth: "token" as const,
  aws_connection_id: null,
};

describe("K8sDeploySettingsDialog", () => {
  beforeEach(() => {
    mutate.mockReset();
    clear.mockReset();
  });

  it("saves a registry, credentials, ingress class, domain, and TLS", async () => {
    const conn = connection({
      id: "conn-k8s",
      kind: "k8s",
      deploy_settings: null,
    });
    render(
      <K8sDeploySettingsDialog connection={conn} open onOpenChange={vi.fn()} />,
    );
    expect(
      screen.queryByRole("button", { name: "Use kind defaults" }),
    ).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Registry"), "ghcr.io/acme");
    await userEvent.type(screen.getByLabelText("Username"), "acme-bot");
    await userEvent.type(screen.getByLabelText("Token"), "ghp_secret");
    await userEvent.click(
      screen.getByRole("checkbox", { name: /imagePullSecret/ }),
    );
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
          registry_auth: "token",
          aws_connection_id: null,
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
    const conn = connection({
      id: "conn-k8s",
      kind: "k8s",
      deploy_settings: SAVED,
    });
    render(
      <K8sDeploySettingsDialog connection={conn} open onOpenChange={vi.fn()} />,
    );
    expect(screen.getByLabelText("Registry")).toHaveValue("ghcr.io/acme");
    expect(screen.getByLabelText("Token")).toHaveValue("");
    expect(
      screen.getByText("Saved. Leave empty to keep it."),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    const body = mutate.mock.calls[0]![0].body;
    expect(body.token).toBeNull();
    expect(body.tls_cluster_issuer).toBe("letsencrypt");
    await userEvent.click(
      screen.getByRole("button", { name: "Use kind defaults" }),
    );
    expect(clear).toHaveBeenCalledWith("conn-k8s", expect.anything());
  });

  it("logs in to Amazon ECR through a deploy-capable AWS connection", async () => {
    const conn = connection({
      id: "conn-k8s",
      kind: "k8s",
      deploy_settings: null,
    });
    render(
      <K8sDeploySettingsDialog connection={conn} open onOpenChange={vi.fn()} />,
    );
    await userEvent.click(
      screen.getByRole("radio", {
        name: "Amazon ECR (through an AWS connection)",
      }),
    );
    expect(screen.queryByLabelText("Username")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("radio", { name: /AWS observe/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    await userEvent.click(
      screen.getByRole("radio", { name: /AWS 123456789012/ }),
    );
    expect(screen.getByLabelText("Registry")).toHaveValue(
      "123456789012.dkr.ecr.us-east-2.amazonaws.com",
    );
    await userEvent.click(
      screen.getByRole("checkbox", { name: /fresh ECR token/ }),
    );
    await userEvent.type(screen.getByLabelText("App domain"), "apps.acme.dev");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(mutate.mock.calls[0]![0].body).toEqual({
      registry: "123456789012.dkr.ecr.us-east-2.amazonaws.com",
      registry_auth: "ecr",
      aws_connection_id: "conn-aws",
      username: null,
      token: null,
      pull_secret: true,
      ingress_class: "nginx",
      app_domain: "apps.acme.dev",
      tls: false,
      tls_cluster_issuer: null,
      public_url_base: null,
    });
  });

  it("opens saved ECR settings with the AWS connection chosen", () => {
    const conn = connection({
      id: "conn-k8s",
      kind: "k8s",
      deploy_settings: {
        ...SAVED,
        registry: "123456789012.dkr.ecr.us-east-2.amazonaws.com",
        username: null,
        token_saved: false,
        registry_auth: "ecr",
        aws_connection_id: "conn-aws",
      },
    });
    render(
      <K8sDeploySettingsDialog connection={conn} open onOpenChange={vi.fn()} />,
    );
    expect(
      screen.getByRole("radio", {
        name: "Amazon ECR (through an AWS connection)",
      }),
    ).toBeChecked();
    expect(
      screen.getByRole("radio", { name: /AWS 123456789012/ }),
    ).toBeChecked();
    expect(ecrRegistry({ account: null, region: "us-east-1" })).toBe("");
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
      registry_auth: "token",
      aws_connection_id: null,
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
