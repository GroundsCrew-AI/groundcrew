import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as fake from "./deploys-fake";
import { deployView } from "./deploy-view";
import { initialTimeline, timelineReducer, type TimelineState } from "./timeline";

const request = {
  workspace_id: "ws-1",
  repo: { connection_id: "gh", repo: "GroundsCrew-AI/some-other-repo", ref: "main" },
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

  it("pushes nothing before approval: the push is the first apply step", async () => {
    const created = fake.createDeploy(request, { ...labels, targetKind: "aws_ecs" });
    const run = follow(created.run_id);
    await vi.runAllTimersAsync();
    const state = run.get();
    const stages = state.steps.flatMap((s) => (s.data?.type === "stage_started" ? [s.data.stage] : []));
    expect(stages).toEqual(["scout", "build", "smoke", "plan", "preflight", "approval"]);
    const plan = deployView(state.steps, state.approvals).plan!;
    expect(plan.resources.slice(0, 2).map((r) => [r.kind, r.action])).toEqual([
      ["ecr:Repository", "create"],
      ["image", "push"],
    ]);
    expect(plan.source_image).toMatch(/^sha256:/);
    expect(plan.preflight[0]?.detail).toMatch(/^Built and smoke-tested \(sha256:/);
    run.stop();
  });

  it("always shows legacy-flask-app as the source", () => {
    const created = fake.createDeploy(request, labels);
    expect(fake.getDeploy(created.deploy_id)?.source.repo).toBe("GroundsCrew-AI/legacy-flask-app");
  });

  it("re-runs only preflight when a missing value is set, with the same image", async () => {
    const created = fake.createDeploy({ ...request, env: [] }, labels);
    const run = follow(created.run_id);
    await vi.runAllTimersAsync();
    const first = deployView(run.get().steps, run.get().approvals);
    expect(first.plan?.preflight.find((c) => c.name === "env")?.status).toBe("fail");
    const attempts = first.attempts.length;

    fake.updateEnv(created.run_id, first.planApproval!.id, {
      env: [{ name: "DATABASE_URL", value: "postgresql://a:hunter2@db/x", secret: true }],
      allow_missing_env: false,
    });
    await vi.runAllTimersAsync();
    const state = run.get();
    const view = deployView(state.steps, state.approvals);
    expect(state.run?.status).toBe("waiting_approval");
    expect(state.approvals[first.planApproval!.id]?.status).toBe("rejected");
    expect(view.planApproval?.status).toBe("pending");
    expect(view.planApproval?.id).not.toBe(first.planApproval!.id);
    expect(view.attempts).toHaveLength(attempts);
    expect(view.plan?.source_image).toBe(first.plan?.source_image);
    expect(view.plan?.preflight.find((c) => c.name === "env")?.status).toBe("pass");
    expect(view.plan?.env.find((v) => v.name === "DATABASE_URL")).toMatchObject({ provided: true, value: null });
    const restarted = state.steps.flatMap((s) => (s.data?.type === "stage_started" ? [s.data.stage] : []));
    expect(restarted.filter((s) => s === "preflight")).toHaveLength(2);
    expect(restarted.filter((s) => s === "build" || s === "smoke")).toHaveLength(2);
    expect(JSON.stringify(state)).not.toContain("hunter2");

    fake.decide(created.run_id, view.planApproval!.id, "approve");
    await vi.runAllTimersAsync();
    expect(run.get().done).toBe(true);
    run.stop();
  });

  it("continues without a missing value by overriding the env check", async () => {
    const created = fake.createDeploy({ ...request, env: [] }, labels);
    const run = follow(created.run_id);
    await vi.runAllTimersAsync();
    const first = deployView(run.get().steps, run.get().approvals);
    fake.updateEnv(created.run_id, first.planApproval!.id, { env: [], allow_missing_env: true });
    await vi.runAllTimersAsync();
    const plan = deployView(run.get().steps, run.get().approvals).plan!;
    expect(plan.allow_missing_env).toBe(true);
    expect(plan.preflight.find((c) => c.name === "env")).toMatchObject({ status: "fail", overridden: true });
    run.stop();
  });

  it("keeps elapsed time consistent with the step durations", async () => {
    const created = fake.createDeploy(request, { ...labels, targetKind: "aws_ecs" });
    const run = follow(created.run_id);
    await vi.runAllTimersAsync();
    const waiting = fake.elapsed(created.run_id)!;
    const build = deployView(run.get().steps, run.get().approvals).stages.find((s) => s.stage === "build")!;
    const attempts = deployView(run.get().steps, run.get().approvals).attempts;
    expect(build.durationMs!).toBeGreaterThanOrEqual(attempts.reduce((sum, a) => sum + a.duration_ms, 0));
    expect(waiting).toBeGreaterThanOrEqual(build.durationMs!);

    fake.decide(created.run_id, deployView(run.get().steps, run.get().approvals).planApproval!.id, "approve");
    await vi.runAllTimersAsync();
    const state = run.get();
    const stages = deployView(state.steps, state.approvals).stages;
    const total = stages.reduce((sum, s) => sum + (s.durationMs ?? 0), 0);
    expect(state.run!.duration_ms).toBeGreaterThanOrEqual(total);
    expect(fake.elapsed(created.run_id)).toBe(state.run!.duration_ms);
    // Step times follow the same clock: the passing build lands about 97 s after the failing one.
    const builds = state.steps.filter((s) => s.data?.type === "build_attempt");
    const gap = Date.parse(builds[1]!.created_at) - Date.parse(builds[0]!.created_at);
    expect(gap).toBeGreaterThanOrEqual(96900);
    run.stop();
  });
});
