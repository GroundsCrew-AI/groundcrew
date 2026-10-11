import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as fake from "./deploys-fake";
import { deployView } from "./deploy-view";
import { initialTimeline, timelineReducer, type TimelineState } from "./timeline";

const request = {
  workspace_id: "ws-1",
  repo: { connection_id: "gh", repo: "GroundsCrew-AI/legacy-flask-app", ref: "main" },
  target_connection_id: "k8s",
  env: [{ name: "DATABASE_URL", value: "postgresql://a:hunter2@db/x", secret: true }],
  options: { allow_missing_env: false },
};
const labels = { repo: "GroundsCrew-AI/legacy-flask-app", ref: "main", targetName: "kind", targetKind: "k8s" as const };

function follow(runId: string) {
  let state: TimelineState = initialTimeline;
  const stop = fake.subscribe(runId, (action) => (state = timelineReducer(state, action)));
  return { get: () => state, stop };
}

describe("preview deploy (TODO(M6): delete with deploys-fake.ts)", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("runs to the approval, then to a live URL after approval", async () => {
    const created = fake.createDeploy(request, labels);
    const run = follow(created.run_id);
    await vi.runAllTimersAsync();
    let state = run.get();
    expect(state.run?.status).toBe("waiting_approval");
    const view = deployView(state.steps, state.approvals);
    expect(view.attempts.map((a) => a.ok)).toEqual([false, true]);
    expect(view.plan?.env.find((v) => v.name === "DATABASE_URL")?.value).toBeNull();
    expect(JSON.stringify(state)).not.toContain("hunter2");

    fake.decide(created.run_id, view.planApproval!.id, "approve");
    await vi.runAllTimersAsync();
    state = run.get();
    expect(state.done).toBe(true);
    expect(deployView(state.steps, state.approvals).liveUrl?.url).toBe("http://legacy-flask-app.localhost");
    expect(fake.getDeploy(created.deploy_id)?.status).toBe("live");
    expect(fake.listDeploys("ws-1").items.map((d) => d.id)).toContain(created.deploy_id);
    run.stop();
  });

  it("stops without deploying when the plan is rejected", async () => {
    const created = fake.createDeploy({ ...request, env: [] }, { ...labels, targetKind: "aws_ecs" });
    const run = follow(created.run_id);
    await vi.runAllTimersAsync();
    const view = deployView(run.get().steps, run.get().approvals);
    expect(view.plan?.preflight.find((c) => c.name === "env")?.status).toBe("fail");
    fake.decide(created.run_id, view.planApproval!.id, "reject");
    await vi.runAllTimersAsync();
    const state = run.get();
    expect(state.run?.status).toBe("stopped");
    expect(deployView(state.steps, state.approvals).liveUrl).toBeNull();
    expect(() => fake.decide(created.run_id, view.planApproval!.id, "approve")).toThrow(/already decided/);
    run.stop();
  });
});
