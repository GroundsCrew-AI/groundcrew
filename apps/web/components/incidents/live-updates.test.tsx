import { act, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DeployStep } from "@/components/deploy/deploy-step";
import type { RunDetail, Step } from "@/lib/api/client";
import { collapseVerifySteps } from "@/lib/incidents";
import { dstep } from "@/tests/deploy-fixtures";
import { runDetail, step as plainStep } from "@/tests/fixtures";

const POST = vi.fn();
const GET = vi.fn();
vi.mock("@/lib/api/client", async (original) => ({
  ...(await original<typeof import("@/lib/api/client")>()),
  api: { POST: (...a: unknown[]) => POST(...a), GET: (...a: unknown[]) => GET(...a) },
}));

type Verify = Extract<NonNullable<Step["data"]>, { type: "verify_result" }>;

function round(seq: number, data: Partial<Verify>): Step {
  return dstep(seq, {
    type: "verify_result",
    ok: false,
    checks: [],
    duration_ms: 5,
    logs_tail: null,
    waiting: null,
    check: seq,
    ...data,
  });
}

const errors = (detail: string, ok = false) =>
  ({ name: "errors", ok, detail, url: null, status_code: null, latency_ms: null }) as const;

describe("Medic verify rounds", () => {
  it("collapses consecutive rounds that say the same thing into the latest", () => {
    const steps = [
      round(1, { waiting: "checkout-api rollout IN_PROGRESS" }),
      round(2, { waiting: "checkout-api rollout IN_PROGRESS" }),
      round(3, { checks: [errors("/ecs/checkout-api: 52 error lines")] }),
      round(4, { checks: [errors("/ecs/checkout-api: 306 error lines")] }),
      round(5, { ok: true }),
      round(6, { ok: true }),
    ];
    const { hidden, rounds } = collapseVerifySteps(steps);
    expect([...hidden]).toEqual([1, 3, 5]);
    expect([...rounds.entries()]).toEqual([
      [2, 2],
      [4, 2],
      [6, 2],
    ]);
  });

  it("says waiting, not Failed, before the deadline, and lists a repeated check once", () => {
    const { rerender } = render(
      <DeployStep step={round(3, { waiting: "checkout-api rollout IN_PROGRESS" })} rounds={3} />,
    );
    expect(screen.getByText("Waiting for checkout-api rollout IN_PROGRESS")).toBeInTheDocument();
    expect(screen.getByText(/check 3/)).toHaveTextContent("check 3 (same for 3 checks)");
    expect(screen.queryByText("Failed")).toBeNull();

    // Two services each report "errors": distinct keys, no React warning; duplicates once.
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    rerender(
      <DeployStep
        step={round(4, {
          checks: [
            errors("checkout-api: 0 tasks exited non-zero", true),
            errors("checkout-worker: 0 tasks exited non-zero", true),
            errors("/ecs/checkout-api: 40 error lines"),
            errors("/ecs/checkout-api: 40 error lines"),
          ],
        })}
      />,
    );
    expect(screen.getByText("Not recovered yet")).toBeInTheDocument();
    expect(screen.getAllByText("/ecs/checkout-api: 40 error lines")).toHaveLength(1);
    expect(error).not.toHaveBeenCalled();
    error.mockRestore();

    // A deploy's single verify still says Failed.
    rerender(<DeployStep step={round(5, { check: null })} />);
    expect(screen.getByText("Failed")).toBeInTheDocument();
  });
});

class BrokenEventSource {
  static instances: BrokenEventSource[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    BrokenEventSource.instances.push(this);
    queueMicrotask(() => this.onerror?.());
  }
  addEventListener() {}
  close() {}
}

describe("useRunStream", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal("EventSource", BrokenEventSource);
    POST.mockResolvedValue({ data: { token: "t", expires_at: "x" }, response: new Response() });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    POST.mockReset();
    GET.mockReset();
  });

  it("polls the run while the stream cannot connect, so the page updates without a reload", async () => {
    const { POLL_FALLBACK_MS, useRunStream } = await import("@/lib/use-run-stream");
    const running: RunDetail = runDetail({ status: "running", steps: [plainStep(1)] });
    const done: RunDetail = runDetail({ status: "succeeded", steps: [plainStep(1), plainStep(2)] });
    GET.mockResolvedValueOnce({ data: running, response: new Response() }).mockResolvedValue({
      data: done,
      response: new Response(),
    });
    const dispatch = vi.fn();
    const { result } = renderHook(() => useRunStream("run-1", { enabled: true, lastSeq: 0, dispatch }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(BrokenEventSource.instances[0].url).toContain("/runs/run-1/events?token=t&after=0");
    expect(dispatch).toHaveBeenCalledWith({ type: "loaded", run: running });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_FALLBACK_MS);
    });
    expect(dispatch).toHaveBeenCalledWith({ type: "loaded", run: done });
    expect(result.current).toBe("closed");
  });
});
