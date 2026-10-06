import type {
  Approval,
  ApprovalStepEvent,
  RunDetail,
  RunStatusEvent,
  Step,
} from "@/lib/api/client";

/**
 * The run page's state: the run's header fields, its steps by seq, and its approvals by id.
 * It starts from GET /runs/{id} and then folds in SSE events. Events can repeat (a reconnect
 * replays from the last seen seq, a refresh reloads), so every action is idempotent.
 */
export type TimelineState = {
  run: RunDetail | null;
  steps: Step[];
  approvals: Record<string, Approval>;
  lastSeq: number;
  done: boolean;
};

export type TimelineAction =
  | { type: "loaded"; run: RunDetail }
  | { type: "step"; step: Step }
  | { type: "approval_requested" | "approval_decided"; event: ApprovalStepEvent }
  | { type: "status"; status: RunStatusEvent }
  | { type: "done"; status: RunStatusEvent }
  | { type: "approval_updated"; approval: Approval };

export const initialTimeline: TimelineState = {
  run: null,
  steps: [],
  approvals: {},
  lastSeq: 0,
  done: false,
};

const TERMINAL = new Set(["succeeded", "failed", "stopped"]);

function addStep(state: TimelineState, step: Step): TimelineState {
  if (state.steps.some((s) => s.seq === step.seq)) return state;
  const steps = [...state.steps, step].sort((a, b) => a.seq - b.seq);
  return { ...state, steps, lastSeq: Math.max(state.lastSeq, step.seq) };
}

function addApproval(state: TimelineState, approval: Approval | null | undefined): TimelineState {
  if (!approval) return state;
  const current = state.approvals[approval.id];
  // A pending copy from an older event must not undo a decision we already know about.
  if (current && current.status !== "pending" && approval.status === "pending") return state;
  return { ...state, approvals: { ...state.approvals, [approval.id]: approval } };
}

function applyStatus(state: TimelineState, status: RunStatusEvent): TimelineState {
  if (!state.run) return state;
  return {
    ...state,
    run: { ...state.run, ...status },
    done: state.done || TERMINAL.has(status.status),
  };
}

export function timelineReducer(state: TimelineState, action: TimelineAction): TimelineState {
  switch (action.type) {
    case "loaded": {
      let next: TimelineState = {
        ...state,
        run: action.run,
        done: state.done || TERMINAL.has(action.run.status),
      };
      for (const step of action.run.steps) next = addStep(next, step);
      for (const approval of action.run.approvals) next = addApproval(next, approval);
      return next;
    }
    case "step":
      return addStep(state, action.step);
    case "approval_requested":
    case "approval_decided":
      return addApproval(addStep(state, action.event.step), action.event.approval);
    case "approval_updated":
      return addApproval(state, action.approval);
    case "status":
      return applyStatus(state, action.status);
    case "done":
      return { ...applyStatus(state, action.status), done: true };
  }
}

export function totalTokens(run: Pick<RunDetail, "prompt_tokens" | "completion_tokens">): number {
  return run.prompt_tokens + run.completion_tokens;
}
