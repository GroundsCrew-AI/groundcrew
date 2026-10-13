import { describe, expect, it } from "vitest";

import { attempt, dstep, plan } from "@/tests/deploy-fixtures";
import { approval, runDetail } from "@/tests/fixtures";

import { hiddenDeploySteps, replacedPlans } from "@/components/deploy/deploy-step";

import { deployView, envDisplay, planBlocked } from "./deploy-view";
import { initialTimeline, timelineReducer } from "./timeline";

const started = (seq: number, stage: "scout" | "build" | "approval", agent: "scout" | "builder" | "deployer") =>
  dstep(seq, { type: "stage_started", stage, agent });
const finished = (seq: number, stage: "scout" | "build", agent: "scout" | "builder", status: "succeeded" | "failed" = "succeeded") =>
  dstep(seq, { type: "stage_finished", stage, agent, status, duration_ms: 100, summary: `${stage} done` });

describe("deployView", () => {
  it("marks stages pending, running, and done in pipeline order", () => {
    const view = deployView([started(1, "scout", "scout"), finished(2, "scout", "scout"), started(3, "build", "builder")], {});
    expect(view.stages.slice(0, 3).map((s) => [s.stage, s.status])).toEqual([
      ["scout", "succeeded"],
      ["build", "running"],
      ["smoke", "pending"],
    ]);
    expect(view.stages.at(-1)?.stage).toBe("verify");
    expect(view.current).toBe("build");
    expect(view.stages[0]?.summary).toBe("scout done");
  });

  it("collects build attempts and sees the automatic fix", () => {
    const view = deployView([dstep(1, attempt(1)), dstep(2, attempt(2))], {});
    expect(view.attempts.map((a) => [a.attempt, a.ok])).toEqual([[1, false], [2, true]]);
    expect(view.automaticFix).toBe(true);
    expect(deployView([dstep(1, attempt(1, { ok: true, dockerfile_source: "repo" }))], {}).automaticFix).toBe(false);
  });

  it("shows the approval stage as waiting while the plan is pending", () => {
    const pending = approval({ id: "ap1" });
    const steps = [
      started(1, "approval", "deployer"),
      dstep(2, plan(), { kind: "approval_requested", approval_id: "ap1" }),
    ];
    const view = deployView(steps, { ap1: pending });
    expect(view.stages.find((s) => s.stage === "approval")?.status).toBe("waiting");
    expect(view.plan?.app).toBe("legacy-flask-app");
    expect(view.planApproval?.id).toBe("ap1");
  });

  it("keeps the deploy plan when a rollback plan follows, and shows the rollback stage", () => {
    const steps = [
      dstep(1, plan()),
      dstep(2, { type: "stage_started", stage: "rollback", agent: "deployer" }),
      dstep(3, plan({ rollback_to_revision: "2" })),
    ];
    const view = deployView(steps, {});
    expect(view.plan?.rollback_to_revision).toBeNull();
    expect(view.stages.at(-1)?.stage).toBe("rollback");
  });

  it("gives the same view when the stream replays steps", () => {
    const steps = [started(1, "scout", "scout"), finished(2, "scout", "scout"), dstep(3, attempt(1))];
    let state = timelineReducer(initialTimeline, { type: "loaded", run: runDetail({ steps }) });
    for (const s of steps) state = timelineReducer(state, { type: "step", step: s });
    expect(state.steps).toHaveLength(3);
    expect(deployView(state.steps, state.approvals)).toEqual(deployView(steps, {}));
  });

  it("reads the live URL, verify result, and the final summary", () => {
    const view = deployView(
      [
        dstep(1, { type: "verify_result", ok: true, checks: [], duration_ms: 5, logs_tail: null, waiting: null, check: null }),
        dstep(2, { type: "live_url", url: "http://legacy-flask-app.localhost", target: "k8s", app: "legacy-flask-app", resource_id: null }),
        dstep(3, {
          type: "run_finished",
          status: "succeeded",
          stop_reason: "completed",
          error: null,
          live_url: "http://legacy-flask-app.localhost",
          build_attempts: 2,
          automatic_fix: true,
          duration_ms: 60000,
          cost_usd: 0.003,
          ultra_equiv_usd: null,
        }),
      ],
      {},
    );
    expect(view.verify?.ok).toBe(true);
    expect(view.liveUrl?.url).toBe("http://legacy-flask-app.localhost");
    expect(view.finished?.automatic_fix).toBe(true);
  });
});

describe("planBlocked and envDisplay", () => {
  it("blocks a failed check unless it was overridden", () => {
    const failed = { name: "env" as const, status: "fail" as const, detail: "Missing DATABASE_URL", overridden: false };
    expect(planBlocked(plan({ preflight: [failed] }))).toBe(true);
    expect(planBlocked(plan({ preflight: [{ ...failed, overridden: true }] }))).toBe(false);
    expect(planBlocked(plan({ preflight: [{ ...failed, status: "warn" }] }))).toBe(false);
  });

  it("never shows a secret value", () => {
    const secret = { name: "DB", secret: true, required: true, provided: true, value: "leak" };
    expect(envDisplay(secret)).toBe("********");
    expect(envDisplay({ ...secret, provided: false })).toBe("not set");
    expect(envDisplay({ ...secret, secret: false, value: "plain" })).toBe("plain");
  });
});

describe("repeated stages and replaced plans", () => {
  it("hides a stage start only once that run of the stage has finished", () => {
    const pf = (seq: number, kind: "start" | "end") =>
      kind === "start"
        ? dstep(seq, { type: "stage_started", stage: "preflight", agent: "deployer" })
        : dstep(seq, { type: "stage_finished", stage: "preflight", agent: "deployer", status: "succeeded", duration_ms: 1, summary: null });
    expect([...hiddenDeploySteps([pf(1, "start"), pf(2, "end"), pf(3, "start")])]).toEqual([1]);
  });

  it("marks every deploy plan but the last as replaced", () => {
    const steps = [
      dstep(1, plan(), { kind: "approval_requested", approval_id: "a1" }),
      dstep(2, plan(), { kind: "approval_requested", approval_id: "a2" }),
      dstep(3, plan({ rollback_to_revision: "1" }), { kind: "approval_requested", approval_id: "r1" }),
    ];
    expect([...replacedPlans(steps)]).toEqual(["a1"]);
  });
});
