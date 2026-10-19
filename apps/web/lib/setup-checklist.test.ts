import { afterEach, describe, expect, it } from "vitest";

import type { DeploySummary } from "@/lib/deploys";
import { connection } from "@/tests/discovery-fixtures";

import { readSetupHidden, setupChecklist, writeSetupHidden } from "./setup-checklist";

const deploy = (status: DeploySummary["status"]) => ({ status }) as DeploySummary;

function done(input: Parameters<typeof setupChecklist>[0]) {
  return Object.fromEntries(setupChecklist(input).steps.map((s) => [s.id, s.done]));
}

describe("setupChecklist", () => {
  it("starts with nothing done and only connecting ready", () => {
    const list = setupChecklist({ connections: [], deploys: [], incidentWatch: true });
    expect(list.doneCount).toBe(0);
    expect(list.complete).toBe(false);
    expect(list.steps.map((s) => [s.id, s.ready])).toEqual([
      ["connect", true],
      ["discover", false],
      ["deploy", false],
      ["watch", false],
    ]);
  });

  it("counts only finished, non-preview connections as connected", () => {
    expect(done({ connections: [connection({ status: "pending", resource_count: 0 })], deploys: [], incidentWatch: true }).connect).toBe(false);
    expect(
      done({ connections: [connection({ kind: "azure", preview: true, resource_count: 0 })], deploys: [], incidentWatch: true }).connect,
    ).toBe(false);
    const list = setupChecklist({ connections: [connection({ resource_count: 0 })], deploys: [], incidentWatch: true });
    expect(list.steps[0]).toEqual({ id: "connect", done: true, ready: true });
    expect(list.steps[1]).toEqual({ id: "discover", done: false, ready: true });
  });

  it("is mapped once a connection has resources, and watching needs the map and the setting", () => {
    const connections = [connection({ resource_count: 4 }), connection({ id: "gh", kind: "github", resource_count: 3 })];
    const on = setupChecklist({ connections, deploys: [], incidentWatch: true });
    expect(on.resources).toBe(7);
    expect(done({ connections, deploys: [], incidentWatch: true })).toEqual({
      connect: true,
      discover: true,
      deploy: false,
      watch: true,
    });
    const off = setupChecklist({ connections, deploys: [], incidentWatch: false });
    expect(off.watchOn).toBe(false);
    expect(off.steps[3]).toEqual({ id: "watch", done: false, ready: true });
  });

  it("knows when discovery ran and found nothing", () => {
    const last = { at: "2026-10-08T09:00:00Z", status: "succeeded" as const, resources: 0, truncated: false };
    expect(
      setupChecklist({ connections: [connection({ resource_count: 0, last_discovery: last })], deploys: [], incidentWatch: true })
        .foundNothing,
    ).toBe(true);
    expect(setupChecklist({ connections: [connection({ last_discovery: last })], deploys: [], incidentWatch: true }).foundNothing).toBe(
      false,
    );
  });

  it("needs a deploy that went live", () => {
    const connections = [connection()];
    expect(done({ connections, deploys: [deploy("failed"), deploy("running")], incidentWatch: true }).deploy).toBe(false);
    const list = setupChecklist({ connections, deploys: [deploy("failed"), deploy("live")], incidentWatch: true });
    expect(list.complete).toBe(true);
    expect(list.doneCount).toBe(4);
  });
});

describe("setup hidden flag", () => {
  afterEach(() => window.localStorage.clear());

  it("is remembered per workspace", () => {
    expect(readSetupHidden("ws-1")).toBe(false);
    writeSetupHidden("ws-1");
    expect(readSetupHidden("ws-1")).toBe(true);
    expect(readSetupHidden("ws-2")).toBe(false);
  });
});
