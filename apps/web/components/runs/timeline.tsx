"use client";

import { cn } from "cn";
import { ChevronRight } from "lucide-react";
import { type ReactNode, useState } from "react";

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import type { Approval, Decision, RunStatus, Step } from "@/lib/api/client";
import type { EnvFix } from "@/lib/deploys";
import { agentName, formatCost, formatDuration, formatTimestamp, formatTokens } from "@/lib/format";

import { DeployPlanCard } from "@/components/deploy/plan-card";
import { DeployStep, deployStepColor, hiddenDeploySteps, replacedPlans } from "@/components/deploy/deploy-step";
import { FixApprovalCard } from "@/components/incidents/fix-approval-card";
import { MedicStep, medicStepColor } from "@/components/incidents/medic-step";
import { isMedicStep } from "@/lib/incidents";

import { ApprovalCard, DecidedBy } from "./approval-card";
import { PolicyTag, TierBadge } from "./badges";

const TIER_COLOR: Record<string, string> = {
  nano: "var(--tier-nano)",
  super: "var(--tier-super)",
  ultra: "var(--tier-ultra)",
};

function nodeColor(step: Step, approval: Approval | undefined): string {
  if (step.kind !== "approval_requested" && step.kind !== "approval_decided") {
    if (isMedicStep(step)) return medicStepColor(step);
    const deploy = deployStepColor(step);
    if (deploy) return deploy;
  }
  switch (step.kind) {
    case "llm":
      return TIER_COLOR[step.model_tier ?? ""] ?? "var(--subtle)";
    case "approval_requested":
      return "var(--status-waiting)";
    case "approval_decided":
      return approval?.status === "rejected" ? "var(--status-failed)" : "var(--status-ok)";
    case "error":
    case "tool_rejected":
      return "var(--status-failed)";
    case "budget":
      return "var(--status-waiting)";
    default:
      return "var(--subtle)";
  }
}

function Meta({ step }: { step: Step }) {
  const cost = formatCost(step.cost_usd);
  const hasTokens = step.prompt_tokens !== null || step.completion_tokens !== null;
  return (
    <span className="tabular flex shrink-0 items-center gap-3 font-mono text-[11.5px] text-muted-foreground">
      {hasTokens && (
        <span title="Prompt tokens in, completion tokens out">
          {formatTokens(step.prompt_tokens)} in <span className="text-subtle">/</span>{" "}
          {formatTokens(step.completion_tokens)} out
        </span>
      )}
      {step.latency_ms !== null && <span>{formatDuration(step.latency_ms)}</span>}
      {cost && <span>{cost}</span>}
    </span>
  );
}

function Summaries({ step }: { step: Step }) {
  const [open, setOpen] = useState(false);
  const output = step.output_summary?.trim();
  const input = step.input_summary?.trim();
  if (!output && !input) return null;
  return (
    <Collapsible open={open} onOpenChange={setOpen} className="mt-1.5">
      {output && !open && (
        <p className="line-clamp-2 font-mono text-[12px] break-words whitespace-pre-wrap text-muted-foreground">
          {output}
        </p>
      )}
      <CollapsibleContent className="space-y-2">
        {input && <Block label="Input">{input}</Block>}
        {output && <Block label="Output">{output}</Block>}
      </CollapsibleContent>
      <CollapsibleTrigger className="mt-1 inline-flex items-center gap-1 rounded text-[12px] text-subtle hover:text-foreground">
        <ChevronRight
          aria-hidden="true"
          className={cn("size-3 transition-transform duration-150", open && "rotate-90")}
        />
        {open ? "Hide details" : "Details"}
      </CollapsibleTrigger>
    </Collapsible>
  );
}

function Block({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <p className="mb-1 text-[11.5px] text-subtle">{label}</p>
      <pre className="max-h-80 overflow-auto rounded-md border bg-background px-3 py-2 font-mono text-[12px] leading-relaxed break-words whitespace-pre-wrap">
        {children}
      </pre>
    </div>
  );
}

function StepTitle({ step }: { step: Step }) {
  switch (step.kind) {
    case "llm":
      return (
        <>
          <span className="font-medium">Model call</span>
          <TierBadge tier={step.model_tier} />
          {step.model_id && (
            <span className="hidden truncate font-mono text-[11.5px] text-subtle sm:inline">{step.model_id}</span>
          )}
        </>
      );
    case "tool":
      return (
        <>
          <span className="font-mono text-[12.5px] font-medium">{step.tool_name}</span>
          <PolicyTag policy={step.tool_policy} />
        </>
      );
    case "tool_rejected":
      return (
        <>
          <span className="font-mono text-[12.5px] font-medium text-muted-foreground line-through decoration-subtle">
            {step.tool_name}
          </span>
          <PolicyTag policy={step.tool_policy} />
          <span className="text-[12.5px] text-[var(--status-failed)]">Not run: rejected</span>
        </>
      );
    case "error":
      return <span className="font-medium text-[var(--status-failed)]">Error</span>;
    case "budget":
      return <span className="font-medium">Budget reached</span>;
    case "stopped":
      return <span className="font-medium">Stopped</span>;
    case "note":
      return <span className="font-medium text-muted-foreground">Note</span>;
    default:
      return <span className="font-medium">{step.kind.replaceAll("_", " ")}</span>;
  }
}

function Rail({ color, last, pulse }: { color: string; last: boolean; pulse?: boolean }) {
  return (
    <div aria-hidden="true" className="relative flex w-5 shrink-0 justify-center">
      {!last && <span className="absolute top-4 bottom-[-4px] w-px bg-rail" />}
      <span
        className={cn("relative mt-[5px] size-[9px] rounded-full border-2 bg-background", pulse && "animate-live")}
        style={{ borderColor: color }}
      />
    </div>
  );
}

/**
 * Medic fixes that cannot be approved yet: fix_id -> the prerequisite fix ("Fix 1: Roll back
 * checkout/database-url ...") that is not applied. A restart before the secret rollback would
 * restart on the bad secret; the API refuses it too.
 */
function waitingFixes(steps: Step[]): Map<string, string> {
  const fixes = new Map<string, { order: number; title: string; depends_on?: string[] }>();
  const applied = new Set<string>();
  for (const s of steps) {
    if (s.kind === "approval_requested" && s.data?.type === "fix_approval") fixes.set(s.data.fix.fix_id, s.data.fix);
    if (s.kind === "fix_applied" && s.data?.type === "fix_applied" && s.data.ok) applied.add(s.data.fix_id);
  }
  const out = new Map<string, string>();
  for (const [id, fix] of fixes) {
    const first = (fix.depends_on ?? []).find((d) => !applied.has(d));
    const prerequisite = first ? fixes.get(first) : undefined;
    if (prerequisite) out.set(id, `Fix ${prerequisite.order}: ${prerequisite.title}`);
  }
  return out;
}

export function Timeline({
  steps,
  approvals,
  status,
  result,
  onDecide,
  deciding,
  animateAfter = 0,
  runAgent,
  onFixEnv,
  fixing = null,
}: {
  /** Deploy runs: set missing env vars or continue without them from the plan card. */
  onFixEnv?: (approval: Approval, fix: EnvFix) => void;
  /** The approval whose env fix is being sent. */
  fixing?: string | null;
  /** The run's own agent. Steps by another agent show its name. */
  runAgent?: string;
  /** Steps with a higher seq arrived live and animate in; older ones render still. */
  animateAfter?: number;
  steps: Step[];
  approvals: Record<string, Approval>;
  status: RunStatus;
  result: string | null;
  onDecide: (approval: Approval, decision: Decision) => void;
  deciding: { id: string; decision: Decision } | null;
}) {
  const working = status === "running" || status === "queued";
  const ended = status === "succeeded" || status === "failed" || status === "stopped";
  const showResult = status === "succeeded" && result;
  // A decision is shown on its approval card, so its own step row would repeat it.
  const carded = new Set(
    steps.filter((s) => s.kind === "approval_requested" && s.approval_id).map((s) => s.approval_id),
  );
  // Deploy runs: a stage's start row is dropped once the stage has finished.
  const hidden = hiddenDeploySteps(steps);
  const fixWaits = waitingFixes(steps);
  const replaced = replacedPlans(steps);
  const visible = steps.filter(
    (s) =>
      !hidden.has(s.seq) &&
      !(s.kind === "approval_decided" && s.approval_id && carded.has(s.approval_id)),
  );

  return (
    <ol aria-label="Run steps" className="relative">
      {visible.map((step, index) => {
        const approval = step.approval_id ? approvals[step.approval_id] : undefined;
        const last = index === visible.length - 1 && !working && !showResult;
        return (
          <li
            key={step.seq}
            className={cn("flex gap-3 pb-5", step.seq > animateAfter && "animate-step-in")}
            data-seq={step.seq}
          >
            <Rail
              color={approval && replaced.has(approval.id) ? "var(--subtle)" : nodeColor(step, approval)}
              last={last}
            />
            <div className="min-w-0 flex-1">
              {step.kind === "approval_requested" && approval && step.data?.type === "deploy_plan" ? (
                <DeployPlanCard
                  plan={step.data}
                  approval={approval}
                  runEnded={ended}
                  replaced={replaced.has(approval.id)}
                  onDecide={(decision) => onDecide(approval, decision)}
                  deciding={deciding?.id === approval.id ? deciding.decision : null}
                  onFixEnv={onFixEnv && ((fix) => onFixEnv(approval, fix))}
                  fixing={fixing === approval.id}
                />
              ) : step.kind === "approval_requested" && approval && step.data?.type === "fix_approval" ? (
                <FixApprovalCard
                  fix={step.data.fix}
                  waitingFor={fixWaits.get(step.data.fix.fix_id) ?? null}
                  approval={approval}
                  runEnded={ended}
                  onDecide={(decision) => onDecide(approval, decision)}
                  deciding={deciding?.id === approval.id ? deciding.decision : null}
                />
              ) : step.kind === "approval_requested" && approval ? (
                <ApprovalCard
                  approval={approval}
                  runEnded={ended}
                  onDecide={(decision) => onDecide(approval, decision)}
                  deciding={deciding?.id === approval.id ? deciding.decision : null}
                />
              ) : step.kind === "approval_decided" && approval ? (
                <div className="pt-px">
                  <DecidedBy approval={approval} />
                </div>
              ) : step.data && isMedicStep(step) ? (
                <MedicStep step={step} />
              ) : step.data ? (
                <DeployStep step={step} />
              ) : (
                <>
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px]">
                    <StepTitle step={step} />
                    {runAgent && step.agent !== runAgent && (
                      <span className="text-[12px] text-subtle">{agentName(step.agent)}</span>
                    )}
                    <time
                      dateTime={step.created_at}
                      title={formatTimestamp(step.created_at)}
                      className="sr-only"
                    >
                      {formatTimestamp(step.created_at)}
                    </time>
                    <span className="ml-auto">
                      <Meta step={step} />
                    </span>
                  </div>
                  {step.kind === "tool_rejected" ? (
                    <p className="mt-1 text-[12.5px] text-muted-foreground">
                      A person rejected this action, so it did not run. The agent was told why.
                    </p>
                  ) : (
                    <Summaries step={step} />
                  )}
                </>
              )}
            </div>
          </li>
        );
      })}
      {working && (
        <li className="flex gap-3 pb-2" aria-live="polite">
          <Rail color="var(--status-running)" last pulse />
          <p className="text-[13px] text-muted-foreground">
            {status === "queued" ? "Waiting for a worker" : "Working"}
          </p>
        </li>
      )}
      {showResult && (
        <li className="animate-step-in flex gap-3">
          <Rail color="var(--status-ok)" last />
          <div className="min-w-0 flex-1">
            <p className="text-[13px] font-medium">Result</p>
            <p className="mt-1 text-[13.5px] leading-relaxed whitespace-pre-wrap">{result}</p>
          </div>
        </li>
      )}
    </ol>
  );
}
