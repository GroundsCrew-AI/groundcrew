import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { connection } from "@/tests/discovery-fixtures";

import { DeployForm, deployTargets, envErrors } from "./deploy-form";

// jsdom has no ResizeObserver; radix Switch and Checkbox measure themselves with it.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
};

const github = connection({ id: "gh", kind: "github", name: "GroundsCrew-AI", repos: ["GroundsCrew-AI/legacy-flask-app"], tier: null });
const kind = connection({ id: "k8s", kind: "k8s", name: "kind groundcrew", tier: null });
const observe = connection({ id: "aws-observe", name: "AWS observe", tier: "observe" });
const deployTier = connection({ id: "aws-deploy", name: "AWS 881050425883", tier: "deploy" });

function setup(connections = [github, kind, observe, deployTier]) {
  const onSubmit = vi.fn();
  render(<DeployForm workspaceId="ws-1" connections={connections} submitting={false} onSubmit={onSubmit} />);
  return onSubmit;
}

describe("DeployForm", () => {
  it("disables observe-tier AWS targets and links to turning deploys on", () => {
    setup();
    expect(screen.getByRole("radio", { name: /AWS observe/ })).toBeDisabled();
    expect(screen.getByRole("radio", { name: /kind groundcrew/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /AWS 881050425883/ })).toBeEnabled();
    expect(screen.getByRole("link", { name: "Turn on deploys" })).toHaveAttribute("href", "/connections");
  });

  it("submits the repo, target, env, and options, with a masked secret input", async () => {
    const onSubmit = setup();
    await userEvent.click(screen.getByRole("radio", { name: /AWS 881050425883/ }));
    await userEvent.click(screen.getByRole("button", { name: /add env var/i }));
    await userEvent.type(screen.getByLabelText("Name"), "DATABASE_URL");
    const value = screen.getByLabelText("Value of DATABASE_URL");
    expect(value).toHaveAttribute("type", "password");
    expect(screen.getByRole("switch", { name: "DATABASE_URL is a secret" })).toBeChecked();
    await userEvent.type(value, "postgresql://x");
    await userEvent.click(screen.getByRole("checkbox"));
    await userEvent.click(screen.getByRole("button", { name: /^deploy$/i }));
    expect(onSubmit).toHaveBeenCalledWith({
      workspace_id: "ws-1",
      repo: { connection_id: "gh", repo: "GroundsCrew-AI/legacy-flask-app", ref: "main" },
      target_connection_id: "aws-deploy",
      env: [{ name: "DATABASE_URL", value: "postgresql://x", secret: true }],
      options: { allow_missing_env: true },
    });
  });

  it("keeps a secret switch the person turned off", async () => {
    setup();
    await userEvent.click(screen.getByRole("button", { name: /add env var/i }));
    await userEvent.type(screen.getByLabelText("Name"), "API");
    const toggle = screen.getByRole("switch");
    expect(toggle).not.toBeChecked();
    await userEvent.click(toggle);
    await userEvent.click(toggle);
    await userEvent.type(screen.getByLabelText("Name"), "_TOKEN");
    expect(toggle).not.toBeChecked();
  });

  it("does not submit with a bad env name, and says why", async () => {
    const onSubmit = setup();
    await userEvent.click(screen.getByRole("button", { name: /add env var/i }));
    await userEvent.type(screen.getByLabelText("Name"), "1BAD");
    await userEvent.click(screen.getByRole("button", { name: /^deploy$/i }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText(/not starting with a digit/)).toBeInTheDocument();
  });

  it("takes a typed owner/name when the connection lists no repositories", async () => {
    const empty = connection({ id: "gh2", kind: "github", name: "GroundsCrew-AI", repos: [], tier: null });
    const onSubmit = setup([empty, kind]);
    const repo = screen.getByPlaceholderText("owner/name");
    await userEvent.type(repo, "not a repo");
    await userEvent.click(screen.getByRole("button", { name: /^deploy$/i }));
    expect(onSubmit).not.toHaveBeenCalled();
    await userEvent.clear(repo);
    await userEvent.type(repo, "GroundsCrew-AI/legacy-flask-app");
    await userEvent.click(screen.getByRole("button", { name: /^deploy$/i }));
    expect(onSubmit.mock.calls[0][0].repo).toEqual({
      connection_id: "gh2",
      repo: "GroundsCrew-AI/legacy-flask-app",
      ref: "main",
    });
  });

  it("asks for a GitHub connection when there is none", () => {
    setup([kind]);
    expect(screen.getByRole("link", { name: "Connect GitHub" })).toBeInTheDocument();
  });
});

describe("helpers", () => {
  it("maps connections to targets", () => {
    expect(deployTargets([github, kind, observe, deployTier]).map((t) => [t.connection.id, t.kind, t.disabled])).toEqual([
      ["k8s", "k8s", false],
      ["aws-observe", "aws_ecs", true],
      ["aws-deploy", "aws_ecs", false],
    ]);
  });

  it("finds duplicate and empty names", () => {
    const rows = [
      { key: 1, name: "A", value: "", secret: false, touched: false },
      { key: 2, name: "A", value: "", secret: false, touched: false },
      { key: 3, name: " ", value: "", secret: false, touched: false },
    ];
    expect(Object.keys(envErrors(rows))).toEqual(["2", "3"]);
  });
});
