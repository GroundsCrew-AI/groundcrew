"use client";

import { cn } from "cn";
import { ArrowDown, Square } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { toast } from "sonner";

import { StatusPill, stopReasonLabel } from "@/components/runs/badges";
import { Timeline } from "@/components/runs/timeline";
import { PageBody, PageHeader } from "@/components/shell/page-header";
import { EmptyState, ErrorState } from "@/components/states/states";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, type Approval, type Decision, type RunDetail } from "@/lib/api/client";
import {
  agentName,
  elapsedMs,
  formatCost,
  formatDuration,
  formatRelative,
  formatTimestamp,
  formatTokens,
} from "@/lib/format";
import { useDecide, useRunDetail, useStopRun } from "@/lib/queries";
import { initialTimeline, timelineReducer, totalTokens } from "@/lib/timeline";

import { RunDetails } from "./run-details";
import { type StreamState, useRunStream } from "@/lib/use-run-stream";

const STOPPABLE = new Set(["queued", "running", "waiting_approval"]);

function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [active]);
  return now;
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11.5px] text-subtle">{label}</dt>
      <dd className="tabular mt-0.5 truncate text-[13px]">{children}</dd>
    </div>
  );
}

function StreamIndicator({ state }: { state: StreamState }) {
  if (state === "closed" || state === "idle") return null;
  const label = state === "live" ? "Live" : state === "connecting" ? "Connecting" : "Reconnecting";
  return (
    <span className="flex items-center gap-1.5 text-[12px] text-muted-foreground" role="status">
      <span
        aria-hidden="true"
        className={cn(
          "size-1.5 rounded-full",
          state === "live" ? "bg-[var(--status-ok)]" : "animate-live bg-[var(--status-waiting)]",
        )}
      />
      {label}
    </span>
  );
}

function StopButton({ runId, onStopped }: { runId: string; onStopped: (run: RunDetail) => void }) {
  const stop = useStopRun();
  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <Button variant="outline" size="sm" disabled={stop.isPending}>
          <Square aria-hidden="true" className="fill-current" />
          Stop
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Stop this run?</AlertDialogTitle>
          <AlertDialogDescription>
            The agent stops before its next step. Pending approvals for this run are rejected.
            You cannot resume a stopped run.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Keep running</AlertDialogCancel>
          <AlertDialogAction
            onClick={() =>
              stop.mutate(runId, {
                onSuccess: (run) => {
                  toast.success("Run stopped");
                  onStopped(run as RunDetail);
                },
                onError: (error) => toast.error(error.message),
              })
            }
          >
            Stop run
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

export function RunView({ runId }: { runId: string }) {
  const detail = useRunDetail(runId);
  const [state, dispatch] = useReducer(timelineReducer, initialTimeline);
  const [loadedSeq, setLoadedSeq] = useState<number | null>(null);
  const decide = useDecide();
  const [deciding, setDeciding] = useState<{ id: string; decision: Decision } | null>(null);

  useEffect(() => {
    if (detail.data) {
      dispatch({ type: "loaded", run: detail.data });
      setLoadedSeq((seq) => seq ?? detail.data.steps.at(-1)?.seq ?? 0);
    }
  }, [detail.data]);

  const run = state.run;
  const stream = useRunStream(runId, {
    enabled: Boolean(run) && !state.done,
    lastSeq: state.lastSeq,
    dispatch,
  });
  const live = run ? STOPPABLE.has(run.status) : false;
  const now = useNow(live);

  // Auto-scroll: follow new steps while the reader is at the bottom; otherwise offer a jump.
  const endRef = useRef<HTMLDivElement>(null);
  const [atBottom, setAtBottom] = useState(true);
  const [unseen, setUnseen] = useState(false);
  useEffect(() => {
    const el = endRef.current;
    if (!el) return;
    const observer = new IntersectionObserver(([entry]) => {
      setAtBottom(entry!.isIntersecting);
      if (entry!.isIntersecting) setUnseen(false);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [run !== null]); // eslint-disable-line react-hooks/exhaustive-deps

  const stepCount = state.steps.length;
  // null until the first load: steps that were already there must not trigger a scroll.
  const lastCount = useRef<number | null>(null);
  useEffect(() => {
    if (loadedSeq === null) return;
    if (lastCount.current !== null && stepCount > lastCount.current) {
      if (atBottom) {
        const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        endRef.current?.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "end" });
      } else {
        setUnseen(true);
      }
    }
    lastCount.current = stepCount;
  }, [stepCount, atBottom, loadedSeq]);

  const onDecide = useCallback(
    (approval: Approval, decision: Decision) => {
      setDeciding({ id: approval.id, decision });
      decide.mutate(
        { approval, decision },
        {
          onSuccess: (updated) => {
            dispatch({ type: "approval_updated", approval: updated });
            toast.success(decision === "approve" ? "Approved. The run continues." : "Rejected");
          },
          onError: (error) => toast.error(error.message),
          onSettled: () => setDeciding(null),
        },
      );
    },
    [decide],
  );

  if (detail.isLoading) {
    return (
      <>
        <PageHeader parent={<Link href="/runs">Runs</Link>} title="Run" />
        <PageBody className="max-w-3xl space-y-4">
          <Skeleton className="h-6 w-2/3" />
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-40 w-full" />
        </PageBody>
      </>
    );
  }
  if (detail.error) {
    const missing = detail.error instanceof ApiError && detail.error.status === 404;
    return (
      <>
        <PageHeader parent={<Link href="/runs">Runs</Link>} title="Run" />
        <PageBody className="max-w-3xl">
          {missing ? (
            <EmptyState title="Run not found">
              It does not exist, or it belongs to a workspace you are not a member of.
            </EmptyState>
          ) : (
            <ErrorState error={detail.error} onRetry={() => void detail.refetch()} />
          )}
        </PageBody>
      </>
    );
  }
  if (!run) return null;

  const elapsed = elapsedMs(run.started_at ?? run.created_at, run.finished_at, now);
  const cost = formatCost(run.cost_usd);
  // The status pill already says "Succeeded" or "Needs approval"; only explain other stops.
  const reason =
    run.stop_reason === "completed" || run.stop_reason === "waiting_approval"
      ? null
      : stopReasonLabel(run.stop_reason);

  return (
    <>
      <PageHeader
        parent={<Link href="/runs" className="hover:text-foreground">Runs</Link>}
        title={`${agentName(run.agent)} run`}
        actions={
          <>
            <StreamIndicator state={stream} />
            {live && (
              <StopButton
                runId={runId}
                onStopped={(stopped) =>
                  dispatch({
                    type: "status",
                    status: {
                      ...stopped,
                      result: stopped.result ?? null,
                      error: stopped.error ?? null,
                    },
                  })
                }
              />
            )}
          </>
        }
      />
      <PageBody className="grid max-w-6xl gap-10 xl:grid-cols-[minmax(0,48rem)_15rem]">
        <div className="min-w-0">
          <div className="space-y-4 border-b pb-5">
            <p className="text-[15px] leading-snug font-medium">{run.goal}</p>
            <dl className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-5">
              <Fact label="Status">
                <StatusPill status={run.status} />
              </Fact>
              <Fact label="Tokens">
                <span className="font-mono">{formatTokens(totalTokens(run))}</span>
              </Fact>
              <Fact label="Cost">{cost ?? <span className="text-muted-foreground">No prices set</span>}</Fact>
              <Fact label={run.finished_at ? "Duration" : "Elapsed"}>{formatDuration(elapsed)}</Fact>
              <Fact label="Started">
                <time dateTime={run.created_at} title={formatTimestamp(run.created_at)}>
                  {formatRelative(run.created_at, now)}
                </time>
              </Fact>
            </dl>
            {(reason || run.error) && (
              <p className={cn("text-[12.5px]", run.status === "failed" ? "text-[var(--status-failed)]" : "text-muted-foreground")}>
                {reason}
                {run.error && <span className="block font-mono text-[12px] break-words">{run.error}</span>}
              </p>
            )}
          </div>

          <div className="pt-6">
            {state.steps.length === 0 && !live ? (
              <p className="text-muted-foreground">This run has no steps.</p>
            ) : (
              <Timeline
                steps={state.steps}
                approvals={state.approvals}
                status={run.status}
                result={run.result ?? null}
                onDecide={onDecide}
                deciding={deciding}
                animateAfter={loadedSeq ?? Number.MAX_SAFE_INTEGER}
                runAgent={run.agent}
              />
            )}
            <div ref={endRef} aria-hidden="true" className="h-px" />
          </div>
        </div>
        <div className="hidden xl:block">
          <div className="sticky top-[4.5rem]">
            <RunDetails run={run} steps={state.steps} />
          </div>
        </div>
      </PageBody>

      {unseen && !atBottom && (
        <div className="pointer-events-none fixed inset-x-0 bottom-6 z-30 flex justify-center md:pl-[232px]">
          <Button
            size="sm"
            variant="secondary"
            className="pointer-events-auto border shadow-sm"
            onClick={() => endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" })}
          >
            <ArrowDown aria-hidden="true" />
            Jump to latest
          </Button>
        </div>
      )}
    </>
  );
}
