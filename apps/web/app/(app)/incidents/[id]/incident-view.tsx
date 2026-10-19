"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Check, ExternalLink, X } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useReducer, useState } from "react";
import { toast } from "sonner";

import { ChangeStrip } from "@/components/incidents/change-strip";
import { Fixes } from "@/components/incidents/fix-list";
import { FollowUpNotes } from "@/components/incidents/followup-note";
import { IncidentStatusPill, SeverityTag } from "@/components/incidents/incident-list";
import { RootCausePanel } from "@/components/incidents/root-cause-panel";
import { Timeline } from "@/components/runs/timeline";
import { PageBody, PageHeader } from "@/components/shell/page-header";
import { useAnnounceChange } from "@/components/shell/shell-context";
import { EmptyState, ErrorState } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, type Approval, type Decision } from "@/lib/api/client";
import { elapsedMs, formatCost, formatDuration, formatRelative, formatTimestamp, formatTokens } from "@/lib/format";
import {
  fixLabel,
  incidentKeys,
  type IncidentDetail,
  OPEN_STATUSES,
  ruleLabel,
  STATUS,
  useIncident,
  useDismissFollowUp,
  useRetryFix,
} from "@/lib/incidents";
import { useDecide, useRunDetail, useStopRun } from "@/lib/queries";
import { initialTimeline, timelineReducer } from "@/lib/timeline";
import { useRunStream } from "@/lib/use-run-stream";

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[12px] text-subtle">{label}</dt>
      <dd className="tabular mt-0.5 text-[13px] [overflow-wrap:anywhere]">{children}</dd>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section aria-label={title} className="space-y-2">
      <h2 className="text-[13px] font-medium">{title}</h2>
      {children}
    </section>
  );
}

function Verify({ incident }: { incident: IncidentDetail }) {
  const v = incident.verify;
  if (!v) return null;
  return (
    <Section title={v.ok ? "Recovery check: healthy" : "Recovery check: still failing"}>
      <ul className="space-y-0.5 text-[12.5px]">
        {v.checks.map((c, i) => (
          <li key={i} className="flex items-baseline gap-1.5">
            {c.ok ? (
              <Check aria-label="passed" className="size-3 shrink-0 text-[var(--status-ok)]" />
            ) : (
              <X aria-label="failed" className="size-3 shrink-0 text-[var(--status-failed)]" />
            )}
            <span className="capitalize">{c.name}</span>
            <span className="text-muted-foreground">{c.detail}</span>
          </li>
        ))}
      </ul>
    </Section>
  );
}

export function IncidentView({ incidentId }: { incidentId: string }) {
  const client = useQueryClient();
  const incident = useIncident(incidentId);
  const runId = incident.data?.run_id ?? "";
  const [state, dispatch] = useReducer(timelineReducer, initialTimeline);
  const [loadedSeq, setLoadedSeq] = useState<number | null>(null);
  const [deciding, setDeciding] = useState<{ id: string; decision: Decision } | null>(null);
  const decide = useDecide();
  const stop = useStopRun();
  const retry = useRetryFix(incidentId);
  const dismiss = useDismissFollowUp(incidentId);
  const [retryError, setRetryError] = useState<{ fixId: string; message: string } | null>(null);

  const detail = useRunDetail(runId, { enabled: Boolean(runId) });
  useEffect(() => {
    if (detail.data) {
      dispatch({ type: "loaded", run: detail.data });
      setLoadedSeq((seq) => seq ?? detail.data.steps.at(-1)?.seq ?? 0);
    }
  }, [detail.data]);
  const stream = useRunStream(runId, { enabled: Boolean(state.run) && !state.done, lastSeq: state.lastSeq, dispatch });
  useAnnounceChange(incident.data ? `Incident status: ${STATUS[incident.data.status].label}` : undefined);

  // Medic's results land in the incident as the run goes: refresh the summary on new steps.
  useEffect(() => {
    if (loadedSeq !== null && state.lastSeq > loadedSeq) {
      void client.invalidateQueries({ queryKey: incidentKeys.detail(incidentId) });
    }
  }, [state.lastSeq, loadedSeq, client, incidentId]);

  const onDecide = useCallback(
    (approval: Approval, decision: Decision) => {
      setDeciding({ id: approval.id, decision });
      decide.mutate(
        { approval, decision },
        {
          onSuccess: (updated) => {
            dispatch({ type: "approval_updated", approval: updated });
            toast.success(decision === "approve" ? "Approved. Medic is applying it." : "Rejected. Nothing was changed.");
          },
          onError: (error) => toast.error(error.message),
          onSettled: () => setDeciding(null),
        },
      );
    },
    [decide],
  );

  const onRetry = useCallback(
    (fixId: string) => {
      setRetryError(null);
      retry.mutate(fixId, {
        onSuccess: async () => {
          toast.success("Planned again. Approve it in the timeline to run it.");
          // The run is open again: reload it and follow its stream.
          const reloaded = await detail.refetch();
          if (reloaded.data) dispatch({ type: "reopened", run: reloaded.data });
        },
        onError: (error) => setRetryError({ fixId, message: error.message }),
      });
    },
    [retry, detail],
  );

  const parent = <Link href="/incidents" className="hover:text-foreground">Incidents</Link>;
  if (incident.isLoading || (incident.data && !state.run && !detail.error && detail.isLoading)) {
    return (
      <>
        <PageHeader parent={parent} title="Incident" />
        <PageBody className="max-w-4xl space-y-4" aria-busy="true">
          <Skeleton className="h-6 w-2/3" />
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-40 w-full" />
        </PageBody>
      </>
    );
  }
  if (incident.error || !incident.data) {
    const missing = incident.error instanceof ApiError && incident.error.status === 404;
    return (
      <>
        <PageHeader parent={parent} title="Incident" />
        <PageBody className="max-w-3xl">
          {missing || !incident.error ? (
            <EmptyState title="Incident not found">
              It does not exist, or it belongs to a workspace you are not a member of.
            </EmptyState>
          ) : (
            <ErrorState error={incident.error} onRetry={() => void incident.refetch()} />
          )}
        </PageBody>
      </>
    );
  }

  const i = incident.data;
  const open = OPEN_STATUSES.includes(i.status);
  const run = state.run;
  const cost = formatCost(i.cost_usd);
  const elapsed = i.duration_ms ?? elapsedMs(i.opened_at, null);

  return (
    <>
      <PageHeader
        parent={parent}
        title={i.title}
        actions={
          <>
            {stream === "reconnecting" && <span role="status" className="text-[12px] text-muted-foreground">Reconnecting</span>}
            {run && (
              <Link href={`/runs/${run.id}`} className="text-[12.5px] text-muted-foreground hover:text-foreground">
                Open as run
              </Link>
            )}
            {open && run && (
              <Button
                size="sm"
                variant="outline"
                disabled={stop.isPending}
                onClick={() =>
                  stop.mutate(run.id, {
                    onSuccess: () => {
                      toast.success("Stopped. Pending fixes were rejected.");
                      void client.invalidateQueries({ queryKey: incidentKeys.detail(incidentId) });
                    },
                    onError: (error) => toast.error(error.message),
                  })
                }
              >
                Stop
              </Button>
            )}
          </>
        }
      />
      <PageBody className="max-w-4xl space-y-6">
        <div className="space-y-4 border-b pb-5">
          <dl className="flex flex-wrap gap-x-8 gap-y-3">
            <Fact label="Status"><IncidentStatusPill status={i.status} /></Fact>
            <Fact label="Severity"><SeverityTag severity={i.severity} /></Fact>
            <Fact label="Detected by">{i.detected_by === "user" ? "A person" : ruleLabel(i.detector_rule)}</Fact>
            <Fact label="Opened">
              <time dateTime={i.opened_at} title={formatTimestamp(i.opened_at)}>{formatRelative(i.opened_at)}</time>
            </Fact>
            {i.time_to_detect_ms !== null && <Fact label="Detected in">{formatDuration(i.time_to_detect_ms)}</Fact>}
            <Fact label={i.resolved_at ? "Resolved in" : "Open for"}>{formatDuration(elapsed)}</Fact>
            <Fact label="Tokens / cost">
              <span className="font-mono">{formatTokens(i.total_tokens)}</span>
              {cost && <span className="text-muted-foreground"> / {cost}</span>}
            </Fact>
          </dl>
          <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[12.5px]">
            <span className="text-muted-foreground">Affected</span>
            {i.affected_resources.map((r) => (
              <Link key={r.id} href={`/map?resource=${r.id}`} className="rounded border px-1.5 py-0.5 hover:bg-accent">
                {r.name}
              </Link>
            ))}
          </p>
          {i.memory && (
            <p className="text-[12.5px] text-muted-foreground">
              Seen before:{" "}
              <Link href={`/incidents/${i.memory.incident_id}`} className="text-foreground underline-offset-4 hover:underline">
                {i.memory.title}
              </Link>{" "}
              ({Math.round(i.memory.similarity * 100)}% similar), reused its {i.memory.reused.join(" and ")}.
            </p>
          )}
          {i.merged_into && (
            <p className="text-[13px]">
              Merged into{" "}
              <Link href={`/incidents/${i.merged_into}`} className="font-medium underline underline-offset-2">
                a newer incident
              </Link>
              <span className="text-muted-foreground">
                : the same outage showed up on a related resource, and Medic carried on there.
              </span>
            </p>
          )}
          {(i.merged_from ?? []).length > 0 && (
            <p className="text-[12.5px] text-muted-foreground">
              Took over{" "}
              {(i.merged_from ?? []).map((id, n) => (
                <span key={id}>
                  {n > 0 && ", "}
                  <Link href={`/incidents/${id}`} className="text-foreground underline underline-offset-2">
                    an earlier incident
                  </Link>
                </span>
              ))}{" "}
              on a related resource (same outage).
            </p>
          )}
          {i.outcome && !i.merged_into && (
            <p className={i.outcome.result === "resolved" ? "text-[13px] text-[var(--status-ok)]" : "text-[13px] text-[var(--status-failed)]"}>
              {i.outcome.result === "resolved" ? "Resolved" : "Not resolved"}
              {i.outcome.reason && <span className="text-muted-foreground">: {i.outcome.reason}</span>}
              {i.fixes_applied.length > 0 && (
                <span className="text-muted-foreground"> ({i.fixes_applied.map(fixLabel).join(", ")})</span>
              )}
            </p>
          )}
          {i.status === "resolved" && (
            <FollowUpNotes
              followups={i.followups ?? []}
              onDismiss={(id) =>
                dismiss.mutate(id, {
                  onSuccess: () => toast.success("Dismissed."),
                  onError: (error) => toast.error(error.message),
                })
              }
              dismissing={dismiss.isPending ? (dismiss.variables ?? null) : null}
            />
          )}
        </div>

        {i.root_cause && <RootCausePanel rootCause={i.root_cause} evidence={i.evidence} />}
        {i.changes.length > 0 && (
          <Section title="What changed">
            <ChangeStrip changes={i.changes} windowStart={i.window.start} windowEnd={i.window.end ?? i.resolved_at} signals={i.signals} firstSignalAt={i.first_signal_at} />
          </Section>
        )}
        <Fixes
          incident={i}
          onRetry={onRetry}
          retrying={retry.isPending ? (retry.variables ?? null) : null}
          retryError={retryError}
        />
        <Verify incident={i} />
        {i.research.length > 0 && (
          <Section title="Research">
            <ul className="space-y-1 text-[12.5px]">
              {i.research.map((r, n) => (
                <li key={`${n}-${r.url}`}>
                  <a href={r.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 underline-offset-4 hover:underline">
                    {r.title}<ExternalLink aria-hidden="true" className="size-3" />
                    <span className="sr-only">(opens in a new tab)</span>
                  </a>
                  <p className="text-muted-foreground">{r.snippet}</p>
                </li>
              ))}
            </ul>
          </Section>
        )}

        <Section title="Timeline">
          {run ? (
            <Timeline
              steps={state.steps}
              approvals={state.approvals}
              status={run.status}
              result={null}
              onDecide={onDecide}
              deciding={deciding}
              animateAfter={loadedSeq ?? Number.MAX_SAFE_INTEGER}
              runAgent="medic"
            />
          ) : detail.error ? (
            <ErrorState error={detail.error} onRetry={() => void detail.refetch()} />
          ) : (
            <p className="text-[13px] text-muted-foreground">No run yet.</p>
          )}
        </Section>
      </PageBody>
    </>
  );
}
