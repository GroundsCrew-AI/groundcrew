import { describe, expect, it } from "vitest";

import { approval, runDetail, step } from "@/tests/fixtures";

import { initialTimeline, type TimelineAction, timelineReducer, type TimelineState } from "./timeline";

function run(actions: TimelineAction[], start: TimelineState = initialTimeline): TimelineState {
  return actions.reduce(timelineReducer, start);
}

const statusEvent = (status: "running" | "waiting_approval" | "succeeded") => ({
  status,
  stop_reason: status === "succeeded" ? ("completed" as const) : null,
  result: status === "succeeded" ? "done" : null,
  error: null,
  prompt_tokens: 300,
  completion_tokens: 60,
  cost_usd: null,
  started_at: "2026-10-06T12:00:00Z",
  finished_at: status === "succeeded" ? "2026-10-06T12:00:09Z" : null,
});

describe("timelineReducer", () => {
  it("starts from the loaded run, sorted by seq", () => {
    const state = run([{ type: "loaded", run: runDetail({ steps: [step(2), step(1)] }) }]);
    expect(state.steps.map((s) => s.seq)).toEqual([1, 2]);
    expect(state.lastSeq).toBe(2);
    expect(state.done).toBe(false);
  });

  it("ignores steps it already has, so a reconnect or refresh never duplicates", () => {
    const loaded = run([{ type: "loaded", run: runDetail({ steps: [step(1), step(2)] }) }]);
    const state = run(
      [
        { type: "step", step: step(2) },
        { type: "step", step: step(3) },
        { type: "step", step: step(3) },
        { type: "loaded", run: runDetail({ steps: [step(1), step(2), step(3)] }) },
      ],
      loaded,
    );
    expect(state.steps.map((s) => s.seq)).toEqual([1, 2, 3]);
    expect(state.lastSeq).toBe(3);
  });

  it("puts out-of-order steps in place", () => {
    const state = run([
      { type: "loaded", run: runDetail() },
      { type: "step", step: step(3) },
      { type: "step", step: step(1) },
      { type: "step", step: step(2) },
    ]);
    expect(state.steps.map((s) => s.seq)).toEqual([1, 2, 3]);
  });

  it("tracks approvals from approval events and never reverts a decision", () => {
    const requested = step(4, { kind: "approval_requested", approval_id: "approval-1" });
    const decided = approval({ status: "approved", decided_by_login: "alice" });
    const state = run([
      { type: "loaded", run: runDetail() },
      { type: "approval_requested", event: { step: requested, approval: approval() } },
      { type: "approval_updated", approval: decided },
      // A late replay of the request carries the old pending copy.
      { type: "approval_requested", event: { step: requested, approval: approval() } },
    ]);
    expect(state.approvals["approval-1"]?.status).toBe("approved");
    expect(state.approvals["approval-1"]?.decided_by_login).toBe("alice");
    expect(state.steps).toHaveLength(1);
  });

  it("applies status updates to the header and finishes on done", () => {
    const state = run([
      { type: "loaded", run: runDetail() },
      { type: "status", status: statusEvent("waiting_approval") },
    ]);
    expect(state.run?.status).toBe("waiting_approval");
    expect(state.run?.prompt_tokens).toBe(300);
    expect(state.done).toBe(false);

    const finished = timelineReducer(state, { type: "done", status: statusEvent("succeeded") });
    expect(finished.done).toBe(true);
    expect(finished.run?.result).toBe("done");
  });

  it("knows a run loaded in a final state is done", () => {
    expect(run([{ type: "loaded", run: runDetail({ status: "stopped" }) }]).done).toBe(true);
  });

  it("follows a reopened run again (a Medic fix retry)", () => {
    const ended = run([{ type: "loaded", run: runDetail({ status: "succeeded", steps: [step(1)] }) }]);
    expect(ended.done).toBe(true);
    const reopened = timelineReducer(ended, {
      type: "reopened",
      run: runDetail({ status: "waiting_approval", steps: [step(1), step(2)] }),
    });
    expect(reopened.done).toBe(false);
    expect(reopened.run?.status).toBe("waiting_approval");
    expect(reopened.steps.map((s) => s.seq)).toEqual([1, 2]);
  });

  it("ignores status before the run is loaded", () => {
    expect(timelineReducer(initialTimeline, { type: "status", status: statusEvent("running") })).toBe(
      initialTimeline,
    );
  });
});
