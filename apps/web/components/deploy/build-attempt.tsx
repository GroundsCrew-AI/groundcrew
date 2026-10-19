"use client";

import { cn } from "cn";
import { Check, ChevronRight, X } from "lucide-react";
import { useState } from "react";

import { TierBadge } from "@/components/runs/badges";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import type { BuildAttempt } from "@/lib/deploys";
import { formatCost, formatDuration, formatTokens } from "@/lib/format";

const SOURCE: Record<BuildAttempt["dockerfile_source"], string> = {
  repo: "Repository Dockerfile",
  generated: "Dockerfile written by Builder",
  fixed: "Dockerfile fixed by Builder",
};

/** Unified diff with added and removed lines tinted; plain text for a whole file. */
export function DiffBlock({ diff, label }: { diff: string; label: string }) {
  const isDiff = /^(---|\+\+\+|@@)/m.test(diff);
  return (
    <pre
      aria-label={label}
      className="max-h-80 overflow-auto rounded-md border bg-background py-2 font-mono text-[12px] leading-relaxed"
    >
      {diff.replace(/\n$/, "").split("\n").map((line, i) => {
        const kind = !isDiff
          ? null
          : line.startsWith("+") && !line.startsWith("+++")
            ? "add"
            : line.startsWith("-") && !line.startsWith("---")
              ? "del"
              : line.startsWith("@@")
                ? "hunk"
                : null;
        return (
          <span
            key={i}
            className={cn(
              "block px-3 whitespace-pre-wrap break-words",
              kind === "add" && "bg-[color-mix(in_oklab,var(--status-ok)_14%,transparent)]",
              kind === "del" && "bg-[color-mix(in_oklab,var(--status-failed)_14%,transparent)]",
              kind === "hunk" && "text-subtle",
            )}
          >
            {kind === "add" && <span className="sr-only">Added: </span>}
            {kind === "del" && <span className="sr-only">Removed: </span>}
            {line || " "}
          </span>
        );
      })}
    </pre>
  );
}

function Section({ title, children, defaultOpen = false }: { title: string; children: React.ReactNode; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="inline-flex items-center gap-1 rounded text-[12px] text-subtle hover:text-foreground">
        <ChevronRight aria-hidden="true" className={cn("size-3 transition-transform duration-150", open && "rotate-90")} />
        {title}
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-1.5">{children}</CollapsibleContent>
    </Collapsible>
  );
}

/** One Docker build in the sandbox: what changed, whether it passed, and what it cost. */
export function BuildAttemptCard({ attempt }: { attempt: BuildAttempt }) {
  const call = attempt.model_call;
  const cost = formatCost(call?.cost_usd);
  return (
    <section
      aria-label={`Build attempt ${attempt.attempt}`}
      className={cn(
        "rounded-lg border bg-card",
        !attempt.ok && "border-[color-mix(in_oklab,var(--status-failed)_35%,var(--border))]",
      )}
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b px-3 py-2 text-[13px]">
        {attempt.ok ? (
          <Check aria-hidden="true" className="size-3.5 text-[var(--status-ok)]" />
        ) : (
          <X aria-hidden="true" className="size-3.5 text-[var(--status-failed)]" />
        )}
        <span className="font-medium">
          Build attempt {attempt.attempt}
          <span className="font-normal text-muted-foreground"> of {attempt.max_attempts ?? 5}</span>
        </span>
        <span className={cn("text-[12.5px]", attempt.ok ? "text-[var(--status-ok)]" : "text-[var(--status-failed)]")}>
          {attempt.ok ? "Passed" : "Failed"}
        </span>
        <span className="tabular ml-auto flex items-center gap-3 font-mono text-[12px] text-muted-foreground">
          {call && (
            <>
              <TierBadge tier={call.model_tier} />
              {call.escalated && <span>escalated</span>}
              <span title="Prompt tokens in, completion tokens out">
                {formatTokens(call.prompt_tokens)} in / {formatTokens(call.completion_tokens)} out
              </span>
              {cost && <span>{cost}</span>}
            </>
          )}
          <span>{formatDuration(attempt.duration_ms)}</span>
        </span>
      </div>
      <div className="space-y-2 px-3 py-2.5">
        <p className="text-[12.5px] text-muted-foreground">{SOURCE[attempt.dockerfile_source]}</p>
        {!attempt.ok && (attempt.error_summary || attempt.failing_step) && (
          <div className="space-y-1">
            {attempt.error_summary && <p className="text-[13px]">{attempt.error_summary}</p>}
            {attempt.failing_step && (
              <p className="font-mono text-[12px] break-words text-muted-foreground">{attempt.failing_step}</p>
            )}
          </div>
        )}
        {attempt.rationale && <p className="text-[12.5px] text-muted-foreground">{attempt.rationale}</p>}
        <Section title={attempt.dockerfile_source === "fixed" ? "What changed" : "Dockerfile"}>
          <DiffBlock diff={attempt.dockerfile_diff} label={`Dockerfile for attempt ${attempt.attempt}`} />
        </Section>
        {attempt.log_tail && (
          <Section title="Build log (end)">
            <pre className="max-h-80 overflow-auto rounded-md border bg-background px-3 py-2 font-mono text-[12px] leading-relaxed break-words whitespace-pre-wrap">
              {attempt.log_tail}
            </pre>
          </Section>
        )}
      </div>
    </section>
  );
}
