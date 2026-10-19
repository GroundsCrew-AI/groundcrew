import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { DeploySummary } from "@/lib/deploys";
import type { Connection } from "@/lib/discovery";
import { readSetupHidden, setupChecklist, writeSetupHidden } from "@/lib/setup-checklist";
import { connection } from "@/tests/discovery-fixtures";

import { GettingStarted, SetupCard } from "./getting-started";

const data = vi.hoisted(() => ({
  connections: [] as Connection[],
  deploys: [] as DeploySummary[],
  incidentWatch: true,
  loads: 0,
}));

vi.mock("@/components/connections/run-discovery-button", () => ({
  RunDiscoveryButton: () => <button type="button">Run discovery</button>,
}));
vi.mock("@/lib/workspace", () => ({ useWorkspace: () => ({ workspace: { id: "ws-1", role: "owner" } }) }));
vi.mock("@/lib/discovery", () => ({
  useConnections: () => {
    data.loads += 1;
    return { data: data.connections };
  },
}));
vi.mock("@/lib/deploys", () => ({ useDeploys: () => ({ data: { items: data.deploys, next_cursor: null } }) }));
vi.mock("@/lib/queries", () => ({
  useWorkspaceConfig: () => ({ data: { incident_watch: data.incidentWatch }, isLoading: false }),
}));

const empty = setupChecklist({ connections: [], deploys: [], incidentWatch: true });

describe("SetupCard", () => {
  it("starts with connecting, and does not offer steps that are not ready", () => {
    render(<SetupCard checklist={empty} canManage />);
    expect(screen.getByText("0 of 4")).toBeInTheDocument();
    const steps = screen.getAllByRole("listitem");
    expect(steps).toHaveLength(4);
    expect(steps[0]).toHaveAttribute("aria-current", "step");
    expect(within(steps[0]!).getByRole("link", { name: "Add a connection" })).toHaveAttribute("href", "/connections");
    expect(screen.queryByRole("button", { name: "Run discovery" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "New deploy" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Echo Ops|test run/i)).not.toBeInTheDocument();
  });

  it("tells members that owners add connections", () => {
    render(<SetupCard checklist={empty} canManage={false} />);
    expect(screen.queryByRole("link", { name: "Add a connection" })).not.toBeInTheDocument();
    expect(screen.getByText(/Only workspace owners can add connections/)).toBeInTheDocument();
  });

  it("moves on to discovery and deploys once connected", () => {
    const list = setupChecklist({ connections: [connection({ resource_count: 0 })], deploys: [], incidentWatch: true });
    render(<SetupCard checklist={list} canManage />);
    const steps = screen.getAllByRole("listitem");
    expect(within(steps[0]!).getByText("(done)")).toBeInTheDocument();
    expect(steps[1]).toHaveAttribute("aria-current", "step");
    expect(within(steps[1]!).getByRole("button", { name: "Run discovery" })).toBeInTheDocument();
    expect(within(steps[2]!).getByRole("link", { name: "New deploy" })).toHaveAttribute("href", "/deploy");
  });

  it("explains a discovery that found nothing", () => {
    const last = { at: "2026-10-08T09:00:00Z", status: "succeeded" as const, truncated: false };
    const list = setupChecklist({
      connections: [connection({ resource_count: 0, last_discovery: last })],
      deploys: [],
      incidentWatch: true,
    });
    render(<SetupCard checklist={list} canManage />);
    expect(screen.getByText(/The last discovery found nothing/)).toBeInTheDocument();
  });

  it("links the map once discovered, and points to Settings when incident watch is off", () => {
    const list = setupChecklist({ connections: [connection({ resource_count: 12 })], deploys: [], incidentWatch: false });
    render(<SetupCard checklist={list} canManage />);
    expect(screen.getByRole("link", { name: "12 resources on the map" })).toHaveAttribute("href", "/map");
    expect(screen.getByText(/Incident watch is off/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Settings" })).toHaveAttribute("href", "/settings");
  });

  it("can be hidden", async () => {
    const onHide = vi.fn();
    render(<SetupCard checklist={empty} canManage onHide={onHide} />);
    await userEvent.click(screen.getByRole("button", { name: "Hide" }));
    expect(onHide).toHaveBeenCalled();
  });
});

describe("GettingStarted", () => {
  afterEach(() => {
    window.localStorage.clear();
    data.connections = [];
    data.deploys = [];
    data.loads = 0;
  });

  it("shows the checklist for a new workspace", () => {
    render(<GettingStarted />);
    expect(screen.getByRole("heading", { name: "Get started" })).toBeInTheDocument();
  });

  it("goes away for good once every step is done", () => {
    data.connections = [connection({ resource_count: 5 })];
    data.deploys = [{ status: "live" } as DeploySummary];
    render(<GettingStarted />);
    expect(screen.queryByRole("heading", { name: "Get started" })).not.toBeInTheDocument();
    expect(readSetupHidden("ws-1")).toBe(true);
  });

  it("loads nothing once hidden for the workspace", () => {
    writeSetupHidden("ws-1");
    render(<GettingStarted />);
    expect(screen.queryByRole("heading", { name: "Get started" })).not.toBeInTheDocument();
    expect(data.loads).toBe(0);
  });

  it("hides when asked", async () => {
    render(<GettingStarted />);
    await userEvent.click(screen.getByRole("button", { name: "Hide" }));
    expect(screen.queryByRole("heading", { name: "Get started" })).not.toBeInTheDocument();
    expect(readSetupHidden("ws-1")).toBe(true);
  });
});
