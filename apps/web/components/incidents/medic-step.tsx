"use client";

import { cn } from "cn";
import { Check, ExternalLink, GitPullRequest, History, Search, X } from "lucide-react";
import Link from "next/link";
import { type ReactNode, useState } from "react";

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import type { Step } from "@/lib/api/client";
import { formatCost, formatDuration } from "@/lib/format";
import { fixLabel, ruleLabel, STATUS } from "@/lib/incidents";

/** Rail colour for an incident step in the shared timeline. */
export function medicStepColor(step: Step): string {
  const data = step.data;
  switch (data?.type) {
    case "incident_opened":
      return "var(--status-failed)";
    case "incident_updated":
      return STATUS[data.status].color;
    case "fix_applied":
      return data.ok ? "var(--status-ok)" : "var(--status-failed)";
    case "incident_resolved":
      return "var(--status-ok)";
    case "incident_failed":
      return "var(--status-failed)";
    case "diagnosis":
      return "var(--tier-ultra)";
    default:
      return "var(--subtle)";
  }
}

function Title({ children, aside }: { children: ReactNode; aside?: ReactNode }) {
  return (
    <p className="flex flex-wrap items-baseline gap-x-2 text-[13px]">
      <span className="font-medium">{children}</span>
      {aside && <span className="text-muted-foreground">{aside}</span>}
    </p>
  );
}

function More({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <Collapsible open={open} onOpenChange={setOpen} className="mt-1">
      <CollapsibleContent className="mt-1">{children}</CollapsibleContent>
      <CollapsibleTrigger className="mt-1 text-[12px] text-subtle hover:text-foreground">
        {open ? "Hide" : label}
      </CollapsibleTrigger>
    </Collapsible>
  );
}

/** An incident event in the run timeline, rendered from its typed `data`. */
export function MedicStep({ step }: { step: Step }) {
  const data = step.data;
  if (!data) return null;
  switch (data.type) {
    case "incident_opened":
      return (
        <div>
          <Title aside={ruleLabel(data.detector_rule)}>Incident opened</Title>
          <p className="mt-0.5 text-[13px]">{data.title}</p>
          <ul className="mt-1 space-y-0.5 text-[12.5px] text-muted-foreground">
            {data.signals.map((s, i) => (
              <li key={i}>
                <span className="text-foreground">{s.resource_name}</span>: {s.detail}
              </li>
            ))}
          </ul>
          {(data.merged_from ?? []).length > 0 && (
            <p className="mt-1 text-[12.5px] text-muted-foreground">
              Took over{" "}
              {(data.merged_from ?? []).map((id, i) => (
                <span key={id}>
                  {i > 0 && ", "}
                  <Link href={`/incidents/${id}`} className="text-foreground underline-offset-4 hover:underline">
                    an incident Medic could not fix
                  </Link>
                </span>
              ))}{" "}
              on a related resource: same outage.
            </p>
          )}
        </div>
      );
    case "incident_updated":
      return data.merged_into ? (
        <div>
          <Title aside={data.reason ?? undefined}>Merged</Title>
          <Link href={`/incidents/${data.merged_into}`} className="text-[13px] underline-offset-4 hover:underline">
            {step.output_summary ?? "Open the newer incident"}
          </Link>
        </div>
      ) : (
        <Title aside={data.reason ?? undefined}>{STATUS[data.status].label}</Title>
      );
    case "evidence_collected":
      return (
        <div>
          <Title aside={`${data.items.length} of ${data.total_lines} lines, ${data.sources.length} sources`}>
            Evidence collected
          </Title>
          {(data.neighbors ?? []).length > 0 && (
            <p className="mt-0.5 text-[12.5px] text-muted-foreground">
              Also read: {(data.neighbors ?? []).map((n) => n.name).join(", ")}
            </p>
          )}
          <More label="Show lines">
            <ul className="max-h-72 space-y-0.5 overflow-auto rounded-md border bg-background px-3 py-2 font-mono text-[11.5px]">
              {data.items.map((item) => (
                <li key={item.id} className="break-words">
                  <span className="text-subtle">{item.timestamp.slice(11, 19)}</span>{" "}
                  <span className="text-muted-foreground">{item.source}</span> {item.line}
                </li>
              ))}
            </ul>
          </More>
        </div>
      );
    case "memory_checked":
      return (
        <div className="flex items-start gap-1.5 text-[13px]">
          <History aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
          {data.reused_incident_id ? (
            <p>
              <span className="font-medium">Seen before.</span>{" "}
              <Link href={`/incidents/${data.reused_incident_id}`} className="underline-offset-4 hover:underline">
                {data.hits[0]?.title}
              </Link>{" "}
              <span className="text-muted-foreground">
                ({Math.round((data.hits[0]?.similarity ?? 0) * 100)}% similar): reusing its {(data.reused ?? []).join(" and ")}
              </span>
            </p>
          ) : (
            <p className="text-muted-foreground">
              {data.hits.length ? `${data.hits.length} similar past incidents, none close enough to reuse` : "No similar past incident"}
            </p>
          )}
        </div>
      );
    case "research_done":
      return (
        <div className="text-[13px]">
          <p className="flex items-center gap-1.5">
            <Search aria-hidden="true" className="size-3.5 text-muted-foreground" />
            <span className="font-medium">Research</span>
            {data.queries[0] && <span className="truncate font-mono text-[12px] text-muted-foreground">{data.queries[0]}</span>}
          </p>
          {data.skipped_reason && <p className="mt-0.5 text-[12.5px] text-muted-foreground">{data.skipped_reason}</p>}
          <ul className="mt-1 space-y-0.5 text-[12.5px]">
            {data.results.map((r) => (
              <li key={r.url}>
                <a href={r.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 underline-offset-4 hover:underline">
                  {r.title}
                  <ExternalLink aria-hidden="true" className="size-3" />
                  <span className="sr-only">(opens in a new tab)</span>
                </a>
              </li>
            ))}
          </ul>
        </div>
      );
    case "diagnosis":
      return <DiagnosisStep data={data} />;
    case "fix_proposed":
      return (
        <div>
          <Title>{data.fixes.length ? `${data.fixes.length} fixes proposed` : "No fix proposed"}</Title>
          <ol className="mt-0.5 list-decimal pl-5 text-[12.5px] text-muted-foreground">
            {data.fixes.map((f) => (
              <li key={f.fix_id}>
                <span className="text-foreground">{f.title}</span>
                {f.kind === "emergency" ? " (emergency action)" : " (pull request)"}
              </li>
            ))}
          </ol>
          {step.output_summary && !data.fixes.length && (
            <p className="mt-0.5 text-[12.5px] text-muted-foreground">{step.output_summary}</p>
          )}
        </div>
      );
    case "fix_applied":
      return (
        <div className="text-[13px]">
          <p className="flex items-center gap-1.5">
            {data.ok ? (
              <Check aria-hidden="true" className="size-3.5 text-[var(--status-ok)]" />
            ) : (
              <X aria-hidden="true" className="size-3.5 text-[var(--status-failed)]" />
            )}
            <span className="font-medium">{data.ok ? "Fix applied" : "Fix failed"}</span>
            {data.action_result && <span className="text-muted-foreground">{data.action_result.summary}</span>}
          </p>
          {data.pull_request && (
            <a href={data.pull_request.url} target="_blank" rel="noreferrer"
              className="mt-0.5 inline-flex items-center gap-1 font-mono text-[12.5px] underline-offset-4 hover:underline">
              <GitPullRequest aria-hidden="true" className="size-3.5" />
              {data.pull_request.repo}#{data.pull_request.number}
              <span className="sr-only">(opens in a new tab)</span>
            </a>
          )}
          {data.error && <p className="mt-0.5 text-[12.5px] text-[var(--status-failed)]">{data.error}</p>}
        </div>
      );
    case "incident_resolved": {
      const cost = formatCost(data.cost_usd);
      return (
        <div className="text-[13px]">
          <p className="font-medium text-[var(--status-ok)]">Resolved</p>
          <p className="mt-0.5 text-[12.5px] text-muted-foreground">
            {data.fixes_applied.map(fixLabel).join(", ")}
            {data.time_to_detect_ms !== null && `, detected in ${formatDuration(data.time_to_detect_ms)}`}
            {`, resolved in ${formatDuration(data.duration_ms)}`}
            {data.memory_reused && ", from incident memory"}
            {cost && `, ${cost} in model calls`}
          </p>
        </div>
      );
    }
    case "incident_failed":
      return (
        <div className="text-[13px]">
          <p className="font-medium text-[var(--status-failed)]">Not resolved</p>
          <p className="mt-0.5 text-[12.5px] text-muted-foreground">{data.reason}</p>
          {(data.still_failing ?? []).length > 0 && (
            <ul className="mt-0.5 list-disc pl-5 text-[12.5px]">
              {(data.still_failing ?? []).map((s) => <li key={s}>{s}</li>)}
            </ul>
          )}
        </div>
      );
    default:
      return null;
  }
}

type DiagnosisData = Extract<NonNullable<Step["data"]>, { type: "diagnosis" }>;

function DiagnosisStep({ data }: { data: DiagnosisData }) {
  const rc = data.root_cause;
  const dropped = data.dropped ?? [];
  return (
    <div className="text-[13px]">
      <Title aside={`confidence ${rc.confidence}`}>Root cause</Title>
      <p className="mt-0.5">{rc.summary}</p>
      {dropped.length > 0 && (
        <p className={cn("mt-0.5 text-[12px] text-muted-foreground")}>
          {dropped.length} uncited {dropped.length === 1 ? "item" : "items"} dropped
        </p>
      )}
    </div>
  );
}

