import type { DeploySummary } from "@/lib/deploys";
import type { Connection } from "@/lib/discovery";

/**
 * The overview's getting-started checklist, worked out from what the workspace already has (its
 * connections, deploys, and settings), so it never asks for something that is done. It goes
 * away once every step is done.
 */

export type SetupStepId = "connect" | "discover" | "deploy" | "watch";

export type SetupStep = {
  id: SetupStepId;
  done: boolean;
  /** Whatever this step needs first is done, so its action can be offered. */
  ready: boolean;
};

export type SetupChecklist = {
  steps: SetupStep[];
  doneCount: number;
  complete: boolean;
  /** A discovery finished but found nothing (wrong region, or access not in place yet). */
  foundNothing: boolean;
  /** The workspace's incident watch setting. */
  watchOn: boolean;
  /** Resources on the map, across connections. */
  resources: number;
};

export type SetupInput = {
  connections: Connection[];
  deploys: DeploySummary[];
  /** The workspace's incident_watch setting (on by default). */
  incidentWatch: boolean;
};

export function setupChecklist({ connections, deploys, incidentWatch }: SetupInput): SetupChecklist {
  const connected = connections.some((c) => c.status === "connected" && !c.preview);
  const resources = connections.reduce((sum, c) => sum + (c.resource_count ?? 0), 0);
  const mapped = resources > 0;
  const deployed = deploys.some((d) => d.status === "live");
  const steps: SetupStep[] = [
    { id: "connect", done: connected, ready: true },
    { id: "discover", done: mapped, ready: connected },
    { id: "deploy", done: deployed, ready: connected },
    // Medic watches the services discovery found, so watching needs the map too.
    { id: "watch", done: incidentWatch && mapped, ready: mapped },
  ];
  const doneCount = steps.filter((s) => s.done).length;
  return {
    steps,
    doneCount,
    complete: doneCount === steps.length,
    foundNothing: !mapped && connections.some((c) => c.last_discovery?.status === "succeeded"),
    watchOn: incidentWatch,
    resources,
  };
}

const DISMISS_PREFIX = "groundcrew.setup-hidden.";

/** Whether someone hid the checklist for this workspace on this browser. */
export function readSetupHidden(workspaceId: string): boolean {
  try {
    return window.localStorage.getItem(DISMISS_PREFIX + workspaceId) === "1";
  } catch {
    return false;
  }
}

export function writeSetupHidden(workspaceId: string): void {
  try {
    window.localStorage.setItem(DISMISS_PREFIX + workspaceId, "1");
  } catch {
    // Private mode or blocked storage: it stays hidden until the page reloads.
  }
}
