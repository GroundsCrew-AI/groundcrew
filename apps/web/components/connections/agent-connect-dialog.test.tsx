import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentJoin, Connection } from "@/lib/discovery";
import { connection } from "@/tests/discovery-fixtures";

import { AgentConnectDialog, installCommand } from "./agent-connect-dialog";
import { ConnectionRow } from "./connection-row";

function agentConnection(agent: Partial<NonNullable<Connection["agent"]>> = {}, overrides: Partial<Connection> = {}): Connection {
  return connection({
    id: "conn-agent",
    kind: "agent",
    name: "web-1",
    status: "pending",
    account: null,
    role_arn: null,
    external_id: null,
    tier: null,
    regions: null,
    agent: {
      enrolled: false,
      online: false,
      hostname: null,
      os: null,
      arch: null,
      version: null,
      enrolled_at: null,
      last_seen_at: null,
      join_expires_at: "2026-10-22T13:00:00Z",
      services: 0,
      latest_version: "0.3.0",
      update_available: false,
      deploy_services: [],
      registry: "groundcrew",
      registry_auth: "groundcrew",
      registry_username: null,
      registry_token_saved: false,
      registry_aws_connection_id: null,
      ...agent,
    },
    ...overrides,
  });
}

const state = vi.hoisted(() => ({ live: [] as Connection[], created: vi.fn() }));

vi.mock("@/lib/workspace", () => ({ useWorkspace: () => ({ workspace: { id: "ws-1", role: "owner" } }) }));
vi.mock("@/lib/discovery", async (original) => ({
  ...(await original<typeof import("@/lib/discovery")>()),
  useConnections: () => ({ data: state.live }),
  useDeleteConnection: () => ({ mutate: vi.fn(), isPending: false }),
  useAgentToken: () => ({ mutate: vi.fn(), reset: vi.fn(), isPending: false, error: null }),
  useConnectAgent: () => ({
    mutate: (body: unknown, opts: { onSuccess: (join: AgentJoin) => void }) => {
      state.created(body);
      opts.onSuccess({
        connection: agentConnection(),
        token: "gcj_secret",
        expires_at: "2026-10-22T13:00:00Z",
        api_url: "https://api.groundkrew.com",
      });
    },
    reset: vi.fn(),
    isPending: false,
    error: null,
  }),
}));

describe("AgentConnectDialog", () => {
  beforeEach(() => {
    state.live = [];
    state.created.mockReset();
  });

  it("builds the install command from this site and the token", () => {
    expect(installCommand("https://groundkrew.com", { token: "gcj_x", api_url: "https://api.groundkrew.com" })).toBe(
      "curl -fsSL https://groundkrew.com/agent/install.sh | sudo sh -s -- --token gcj_x --url https://api.groundkrew.com",
    );
  });

  it("names the agent, shows the one-time install command, then waits for enrollment", async () => {
    const { rerender } = render(<AgentConnectDialog open onOpenChange={vi.fn()} />);
    await userEvent.type(screen.getByLabelText("Name"), "web-1");
    await userEvent.click(screen.getByRole("button", { name: "Create join token" }));
    expect(state.created).toHaveBeenCalledWith({ workspace_id: "ws-1", name: "web-1" });
    expect(screen.getByText(/--token gcj_secret --url https:\/\/api.groundkrew.com/)).toBeInTheDocument();
    expect(screen.getByText(/The token works once/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Waiting for the agent to enroll");

    state.live = [agentConnection({ enrolled: true, online: true, hostname: "web-1.example", enrolled_at: "2026-10-22T12:10:00Z" })];
    rerender(<AgentConnectDialog open onOpenChange={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent("Connected: web-1.example");
  });
});

describe("ConnectionRow for an agent", () => {
  it("shows waiting, online, and offline agents", () => {
    const { rerender } = render(<ConnectionRow connection={agentConnection()} onRefresh={vi.fn()} refreshing={false} />);
    expect(screen.getByText("Waiting for the agent")).toBeInTheDocument();
    expect(screen.getByText("Install the agent with its join token")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New join token" })).toBeEnabled();

    const online = agentConnection(
      { enrolled: true, online: true, hostname: "web-1.example", version: "0.1.0", services: 3 },
      { status: "connected" },
    );
    rerender(<ConnectionRow connection={online} onRefresh={vi.fn()} refreshing={false} />);
    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect(screen.getByText("web-1.example · 3 services · agent 0.1.0")).toBeInTheDocument();

    const offline = agentConnection(
      { enrolled: true, online: false, hostname: "web-1.example", services: 1, last_seen_at: new Date().toISOString() },
      { status: "connected" },
    );
    rerender(<ConnectionRow connection={offline} onRefresh={vi.fn()} refreshing={false} />);
    expect(screen.getByText("Offline")).toBeInTheDocument();
    expect(screen.getByText(/web-1.example · 1 service · last seen/)).toBeInTheDocument();

    const old = agentConnection(
      { enrolled: true, online: true, hostname: "web-1.example", version: "0.1.0", services: 1, update_available: true },
      { status: "connected" },
    );
    rerender(<ConnectionRow connection={old} onRefresh={vi.fn()} refreshing={false} />);
    expect(screen.getByText("web-1.example · 1 service · agent 0.1.0 (0.3.0 available)")).toBeInTheDocument();
    expect(screen.getByText(/Update the agent to 0.3.0/)).toHaveTextContent(
      "curl -fsSL https://groundkrew.com/agent/install.sh | sudo sh",
    );
  });
});
