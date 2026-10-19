"use client";

import { ExternalLink } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useReducer, useState } from "react";
import { toast } from "sonner";

import { DeployStatePill } from "@/components/deploy/deploy-list";
import { StageStrip } from "@/components/deploy/stage-strip";
import { Timeline } from "@/components/runs/timeline";
import { statusLabel } from "@/components/runs/badges";
import { PageBody, PageHeader } from "@/components/shell/page-header";
import { useAnnounceChange } from "@/components/shell/shell-context";
import { EmptyState, ErrorState } from "@/components/states/states";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, type Approval, type Decision, type RunDetail } from "@/lib/api/client";
import { type DeployView, deployView } from "@/lib/deploy-view";
import { type DeployState, type EnvFix, targetLabel, useDeploy, useUpdateEnv } from "@/lib/deploys";
import { elapsedMs, formatCost, formatDuration, formatTokens } from "@/lib/format";
import { useDecide, useRunDetail } from "@/lib/queries";
import { initialTimeline, timelineReducer, totalTokens } from "@/lib/timeline";
import { type StreamState, useRunStream } from "@/lib/use-run-stream";

/** The deploy's status from the live run, so the header changes with the stream. */
export function deployState(run: RunDetail, view: DeployView): DeployState {
  if (view.finished?.status === "succeeded" && view.finished.live_url) return "live";
  if (view.stages.some((s) => s.stage === "rollback" && s.status === "succeeded")) return "rolled_back";
  if (run.status === "waiting_approval") return "waiting_approval";
  if (run.status === "succeeded") return view.liveUrl ? "live" : "stopped";
  if (run.status === "failed") return "failed";
  if (run.status === "stopped") return "stopped";
  return run.status === "queued" ? "queued" : "running";
}

function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [active]);
  return now;
}

function Fact({ label, children, title }: { label: string; children: React.ReactNode; title?: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-[12px] text-subtle">{label}</dt>
      <dd className="tabular mt-0.5 text-[13px] [overflow-wrap:anywhere]" title={title}>
        {children}
      </dd>
    </div>
  );
}

function StreamNote({ state }: { state: StreamState }) {
  if (state !== "reconnecting") return null;
  return (
    <span role="status" className="text-[12px] text-muted-foreground">
      Reconnecting
    </span>
  );
}

export function DeployRunView({ deployId }: { deployId: string }) {
  const deploy = useDeploy(deployId);
  const runId = deploy.data?.run_id ?? "";
  const [state, dispatch] = useReducer(timelineReducer, initialTimeline);
  const [loadedSeq, setLoadedSeq] = useState<number | null>(null);
  const [deciding, setDeciding] = useState<{ id: string; decision: Decision } | null>(null);
  const [fixing, setFixing] = useState<string | null>(null);
  const decide = useDecide();
  const updateEnv = useUpdateEnv(deployId);

  // The run as stored, then the live stream.
  const detail = useRunDetail(runId, { enabled: Boolean(runId) });
  useEffect(() => {
    if (detail.data) {
      dispatch({ type: "loaded", run: detail.data });
      setLoadedSeq((seq) => seq ?? detail.data.steps.at(-1)?.seq ?? 0);
    }
  }, [detail.data]);
  const stream = useRunStream(runId, {
    enabled: Boolean(state.run) && !state.done,
    lastSeq: state.lastSeq,
    dispatch,
  });

  const view = useMemo(() => deployView(state.steps, state.approvals), [state.steps, state.approvals]);
  const run = state.run;
  const live = run ? ["queued", "running", "waiting_approval"].includes(run.status) : false;
  const now = useNow(live);
  useAnnounceChange(run ? `Deploy run status: ${statusLabel(run.status)}` : undefined);

  const onDecide = useCallback(
    (approval: Approval, decision: Decision) => {
      setDeciding({ id: approval.id, decision });
      decide.mutate(
        { approval, decision },
        {
          onSuccess: (updated) => {
            dispatch({ type: "approval_updated", approval: updated });
            toast.success(decision === "approve" ? "Approved. Deploying." : "Rejected. Nothing was changed.");
          },
          onError: (error) => toast.error(error.message),
          onSettled: () => setDeciding(null),
        },
      );
    },
    [decide],
  );

  const onFixEnv = useCallback(
    (approval: Approval, fix: EnvFix) => {
      setFixing(approval.id);
      updateEnv.mutate(
        { approvalId: approval.id, fix },
        {
          onSuccess: () =>
            toast.success(fix.env?.length ? "Value saved. Running preflight again." : "Running preflight again."),
          onError: (error) => toast.error(error.message),
          onSettled: () => setFixing(null),
        },
      );
    },
    [updateEnv],
  );

  const parent = <Link href="/deploy" className="hover:text-foreground">Deploy</Link>;
  if (deploy.isLoading || detail.isLoading || (deploy.data && !run && !detail.error)) {
    return (
      <>
        <PageHeader parent={parent} title="Deploy" />
        <PageBody className="max-w-3xl space-y-4" aria-busy="true">
          <Skeleton className="h-6 w-2/3" />
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-40 w-full" />
        </PageBody>
      </>
    );
  }
  const error = deploy.error ?? detail.error;
  if (error || !deploy.data || !run) {
    const missing = !error || (error instanceof ApiError && error.status === 404);
    return (
      <>
        <PageHeader parent={parent} title="Deploy" />
        <PageBody className="max-w-3xl">
          {missing ? (
            <EmptyState title="Deploy not found">
              It does not exist, or it belongs to a workspace you are not a member of.
            </EmptyState>
          ) : (
            <ErrorState error={error} onRetry={() => void (deploy.error ? deploy.refetch() : detail.refetch())} />
          )}
        </PageBody>
      </>
    );
  }

  const d = deploy.data;
  const status = deployState(run, view);
  const elapsed = elapsedMs(run.started_at ?? run.created_at, run.finished_at, now);
  const cost = formatCost(run.cost_usd);
  const ended = ["succeeded", "failed", "stopped"].includes(run.status);

  return (
    <>
      <PageHeader
        parent={parent}
        title={`${d.app ?? d.source.repo} to ${d.target.name}`}
        actions={
          <>
            <StreamNote state={stream} />
            <Link href={`/runs/${run.id}`} className="text-[12.5px] text-muted-foreground hover:text-foreground">
              Open as run
            </Link>
          </>
        }
      />
      <PageBody className="max-w-4xl space-y-6">
        <div className="space-y-4 border-b pb-5">
          <StageStrip stages={view.stages} current={view.current} />
          <dl className="flex flex-wrap gap-x-8 gap-y-3">
            <Fact label="Status">
              <DeployStatePill state={status} />
            </Fact>
            {/* Never truncated: the repo name wraps instead. */}
            <Fact label="Source" title={`${d.source.repo}${d.source.ref ? `@${d.source.ref}` : ""}`}>
              {d.source.repo}
              {d.source.ref && <span className="text-muted-foreground">@{d.source.ref}</span>}
            </Fact>
            <Fact label="Target">{targetLabel(d.target.kind)}</Fact>
            <Fact label="Build attempts">
              {view.attempts.length}
              {view.automaticFix && <span className="text-muted-foreground">, fixed</span>}
            </Fact>
            <Fact label="Tokens / cost">
              <span className="font-mono">{formatTokens(totalTokens(run))}</span>
              {cost && <span className="text-muted-foreground"> / {cost}</span>}
            </Fact>
            <Fact label={run.finished_at ? "Duration" : "Elapsed"}>{formatDuration(elapsed)}</Fact>
          </dl>
          {view.liveUrl && (
            <p className="flex items-center gap-2 text-[13px]">
              <span className="text-muted-foreground">Live at</span>
              <a href={view.liveUrl.url} target="_blank" rel="noreferrer"
                className="inline-flex items-center gap-1 font-mono underline-offset-4 hover:underline">
                {view.liveUrl.url}
                <ExternalLink aria-hidden="true" className="size-3" />
                <span className="sr-only">(opens in a new tab)</span>
              </a>
            </p>
          )}
          {run.error && <p className="text-[12.5px] text-[var(--status-failed)]">{run.error}</p>}
        </div>

        <Timeline
          steps={state.steps}
          approvals={state.approvals}
          status={run.status}
          result={null}
          onDecide={onDecide}
          deciding={deciding}
          animateAfter={loadedSeq ?? Number.MAX_SAFE_INTEGER}
          runAgent="deploy"
          onFixEnv={onFixEnv}
          fixing={fixing}
        />
        {ended && state.steps.length === 0 && <p className="text-muted-foreground">This deploy has no steps.</p>}
      </PageBody>
    </>
  );
}
