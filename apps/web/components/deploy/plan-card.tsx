"use client";

import { cn } from "cn";
import { AlertTriangle, Check, ChevronRight, KeyRound, X } from "lucide-react";
import { useId, useState } from "react";

import { DecidedBy } from "@/components/runs/approval-card";
import { PolicyTag } from "@/components/runs/badges";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Approval, Decision } from "@/lib/api/client";
import { ENV_CHANGE, envDisplay, failingChecks } from "@/lib/deploy-view";
import { type DeployPlan, type EnvFix, targetLabel } from "@/lib/deploys";
import { formatRelative, formatTimestamp } from "@/lib/format";

const CHECK_COLOR = { pass: "var(--status-ok)", warn: "var(--status-waiting)", fail: "var(--status-failed)" };
const CHECK_NAME: Record<DeployPlan["preflight"][number]["name"], string> = {
  image: "Image",
  env: "Env vars",
  port: "Port",
  name_collision: "Names",
  quota: "Quota",
  other: "Other",
};

/**
 * The approval card for a deploy (or a rollback): the exact rendered plan, with every secret
 * value masked. Approve is disabled while a preflight check fails and was not overridden.
 * When only the env check fails, the card can set the missing values or continue without
 * them (`onFixEnv`): preflight runs again with the same built image and a new plan replaces
 * this one (`replaced`).
 */
export function DeployPlanCard({
  plan,
  approval,
  onDecide,
  deciding,
  runEnded = false,
  replaced = false,
  onFixEnv,
  fixing = false,
}: {
  plan: DeployPlan;
  approval: Approval;
  onDecide: (decision: Decision) => void;
  deciding: Decision | null;
  runEnded?: boolean;
  replaced?: boolean;
  onFixEnv?: (fix: EnvFix) => void;
  fixing?: boolean;
}) {
  const [showRendered, setShowRendered] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const pending = approval.status === "pending";
  const open = pending && !runEnded;
  const failing = failingChecks(plan);
  const blocked = failing.length > 0;
  const missing = plan.env.filter((v) => v.required && !v.provided).map((v) => v.name);
  // Fixable here only when the env check is the one in the way.
  const fixable = open && blocked && Boolean(onFixEnv) && failing.every((c) => c.name === "env") && missing.length > 0;
  const busy = deciding !== null || fixing;
  const rollback = Boolean(plan.rollback_to_revision);
  const title = rollback ? "Roll back" : `Deploy ${plan.app} to ${targetLabel(plan.target)}`;
  const blockedReason = failing.map((c) => `${CHECK_NAME[c.name]}: ${c.detail}`).join("; ");

  if (replaced) {
    return (
      <section aria-label="Replaced deploy plan" className="rounded-lg border border-dashed px-3 py-2 text-[12.5px]">
        <p className="flex flex-wrap items-baseline gap-x-2">
          <span className="font-medium text-[13px]">{title}</span>
          <span className="text-muted-foreground">Replaced</span>
        </p>
        <p className="mt-0.5 text-muted-foreground">
          The env vars changed, so preflight ran again and the plan below replaced this one. The image was not
          rebuilt.
        </p>
      </section>
    );
  }

  return (
    <section
      aria-label={rollback ? "Rollback approval" : "Deploy plan approval"}
      className={cn(
        "rounded-lg border bg-card",
        open && "border-[color-mix(in_oklab,var(--status-waiting)_45%,var(--border))]",
        open && rollback && "border-[color-mix(in_oklab,var(--status-failed)_45%,var(--border))]",
      )}
    >
      <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
        <span className="text-[13px] font-medium">{open ? "Approval needed" : "Approval"}</span>
        <PolicyTag policy={approval.policy} />
        <time
          dateTime={approval.created_at}
          title={formatTimestamp(approval.created_at)}
          className="ml-auto text-[12px] text-muted-foreground"
        >
          Requested {formatRelative(approval.created_at)}
        </time>
      </div>

      <div className="space-y-4 px-3 py-3">
        <div className="space-y-1">
          <h3 className="text-[14px] font-medium">{title}</h3>
          <p className="text-[12.5px] text-muted-foreground">
            {rollback
              ? `Goes back to revision ${plan.rollback_to_revision}.`
              : "Nothing touches infrastructure until you approve this exact plan."}
          </p>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-1.5 pt-2 text-[12.5px] sm:grid-cols-4">
            <PlanFact label="Image" mono wide>{plan.image}</PlanFact>
            <PlanFact label="Port">{plan.port}</PlanFact>
            <PlanFact label="Health">{plan.health_path ?? "none"}</PlanFact>
            <PlanFact label="Replicas">{plan.replicas}</PlanFact>
            <PlanFact label="CPU / memory">{`${plan.cpu} / ${plan.memory}`}</PlanFact>
            {plan.expected_url && <PlanFact label="URL" mono>{plan.expected_url}</PlanFact>}
          </dl>
        </div>

        <div>
          <h4 className="mb-1.5 text-[12px] text-subtle">Resources</h4>
          <ul className="space-y-1 text-[12.5px]">
            {plan.resources.map((r) => (
              <li key={`${r.kind}/${r.name}`} className="flex flex-wrap items-baseline gap-x-2">
                <span className="w-14 shrink-0 font-mono text-[12px] text-muted-foreground">{r.action}</span>
                <span className="text-muted-foreground">{r.kind}</span>
                <span className="font-mono">{r.name}</span>
                {r.detail && <span className="text-muted-foreground">{r.detail}</span>}
              </li>
            ))}
          </ul>
        </div>

        {plan.env.length > 0 && (
          <div>
            <h4 className="mb-1.5 text-[12px] text-subtle">Env vars</h4>
            <table className="w-full text-left text-[12.5px]">
              <caption className="sr-only">Env vars in this plan. Secret values are never shown.</caption>
              <thead className="sr-only">
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Value</th>
                  <th scope="col">Kind</th>
                </tr>
              </thead>
              <tbody>
                {plan.env.map((v) => {
                  const canSet = fixable && v.required && !v.provided;
                  return (
                    <tr key={v.name} className="align-baseline">
                      <td className="py-0.5 pr-3 font-mono">
                        <span className={cn(v.change === "remove" && "line-through")}>{v.name}</span>
                        {v.change && v.change !== "keep" && (
                          <span className="ml-2 font-sans text-[11.5px] text-muted-foreground">{ENV_CHANGE[v.change]}</span>
                        )}
                      </td>
                      <td className="py-0.5 pr-3">
                        {editing === v.name && onFixEnv ? (
                          <EnvValueForm
                            name={v.name}
                            busy={busy}
                            onCancel={() => setEditing(null)}
                            onSave={(value) => {
                              setEditing(null);
                              onFixEnv({
                                env: [{ name: v.name, value, secret: v.secret }],
                                allow_missing_env: plan.allow_missing_env,
                              });
                            }}
                          />
                        ) : (
                          <span className="flex flex-wrap items-baseline gap-x-2">
                            <span
                              className={cn(
                                "font-mono break-all",
                                !v.provided && v.change !== "remove" && "text-[var(--status-failed)]",
                                (v.change === "keep" || v.change === "remove") && "font-sans text-muted-foreground",
                              )}
                            >
                              {envDisplay(v)}
                            </span>
                            {canSet && (
                              <Button
                                size="xs"
                                variant="outline"
                                disabled={busy}
                                onClick={() => setEditing(v.name)}
                                aria-label={`Set a value for ${v.name}`}
                              >
                                <KeyRound aria-hidden="true" />
                                Set value
                              </Button>
                            )}
                          </span>
                        )}
                      </td>
                      <td className="py-0.5 text-muted-foreground">
                        {[v.secret ? "secret" : "plain", v.required ? "required" : null].filter(Boolean).join(", ")}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        <div>
          <h4 className="mb-1.5 text-[12px] text-subtle">Preflight</h4>
          <ul className="space-y-1 text-[12.5px]">
            {plan.preflight.map((c) => (
              <li key={c.name} className="flex items-baseline gap-2">
                <span aria-hidden="true" className="size-1.5 shrink-0 translate-y-[-1px] rounded-full" style={{ background: CHECK_COLOR[c.status] }} />
                <span className="w-16 shrink-0">{CHECK_NAME[c.name]}</span>
                <span className="text-muted-foreground">
                  <span className="sr-only">{c.status}: </span>
                  {c.detail}
                  {c.overridden && " (allowed: deploy with missing env)"}
                </span>
              </li>
            ))}
          </ul>
        </div>

        <Collapsible open={showRendered} onOpenChange={setShowRendered}>
          <CollapsibleTrigger className="inline-flex items-center gap-1 rounded text-[12px] text-subtle hover:text-foreground">
            <ChevronRight aria-hidden="true" className={cn("size-3 transition-transform duration-150", showRendered && "rotate-90")} />
            {showRendered ? "Hide" : "Show"} the rendered {plan.rendered_format === "yaml" ? "manifests" : "task definition"}
          </CollapsibleTrigger>
          <CollapsibleContent className="mt-1.5">
            <pre className="max-h-96 overflow-auto rounded-md border bg-background px-3 py-2 font-mono text-[12px] leading-relaxed">
              {plan.rendered}
            </pre>
          </CollapsibleContent>
        </Collapsible>

        {open ? (
          <div className="space-y-2">
            {blocked && (
              <p
                id={`${approval.id}-blocked`}
                className="flex items-start gap-1.5 text-[12.5px] text-[var(--status-failed)]"
                role="alert"
              >
                <AlertTriangle aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
                {fixable ? (
                  <span>
                    {missing.join(", ")} {missing.length === 1 ? "is" : "are"} required and not set. Set{" "}
                    {missing.length === 1 ? "a value" : "the values"}, or continue without{" "}
                    {missing.length === 1 ? "it" : "them"}. Only preflight runs again; the image is not rebuilt.
                  </span>
                ) : (
                  <span>
                    A preflight check failed, so this plan cannot be approved. Reject it and deploy again with
                    the missing values, or with &quot;Deploy with missing env vars&quot; checked.
                  </span>
                )}
              </p>
            )}
            {fixing && (
              <p role="status" className="text-[12.5px] text-muted-foreground">
                Running preflight again with the same image
              </p>
            )}
            <div className="flex flex-wrap items-center gap-2">
              {blocked ? (
                <Tooltip>
                  <TooltipTrigger asChild>
                    {/* A disabled button gets no pointer events, so the wrapper carries the tooltip. */}
                    <span
                      tabIndex={0}
                      aria-describedby={`${approval.id}-blocked`}
                      className="inline-flex cursor-not-allowed rounded-[min(var(--radius-md),12px)] outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
                    >
                      <Button
                        size="sm"
                        disabled
                        className="border-border bg-muted text-muted-foreground hover:bg-muted disabled:opacity-100"
                      >
                        <Check aria-hidden="true" />
                        {rollback ? "Approve rollback" : "Approve and deploy"}
                      </Button>
                    </span>
                  </TooltipTrigger>
                  <TooltipContent>Blocked by preflight. {blockedReason}</TooltipContent>
                </Tooltip>
              ) : (
                <Button size="sm" onClick={() => onDecide("approve")} disabled={busy}>
                  <Check aria-hidden="true" />
                  {deciding === "approve" ? "Approving" : rollback ? "Approve rollback" : "Approve and deploy"}
                </Button>
              )}
              {fixable && onFixEnv && (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy}
                  onClick={() => onFixEnv({ env: [], allow_missing_env: true })}
                >
                  Continue without {missing.length === 1 ? missing[0] : "them"}
                </Button>
              )}
              <Button size="sm" variant="outline" onClick={() => onDecide("reject")} disabled={busy}>
                <X aria-hidden="true" />
                {deciding === "reject" ? "Rejecting" : "Reject"}
              </Button>
            </div>
          </div>
        ) : pending ? (
          <p className="text-[12.5px] text-muted-foreground">The run ended before anyone decided.</p>
        ) : (
          <DecidedBy approval={approval} />
        )}
      </div>
    </section>
  );
}

/** Masked input for one missing env value. The value is sent once and never shown again. */
function EnvValueForm({
  name,
  busy,
  onSave,
  onCancel,
}: {
  name: string;
  busy: boolean;
  onSave: (value: string) => void;
  onCancel: () => void;
}) {
  const id = useId();
  const [value, setValue] = useState("");
  return (
    <form
      className="flex flex-wrap items-center gap-1.5"
      onSubmit={(e) => {
        e.preventDefault();
        if (value) onSave(value);
      }}
    >
      <label htmlFor={id} className="sr-only">
        Value for {name}
      </label>
      <Input
        id={id}
        type="password"
        autoComplete="off"
        spellCheck={false}
        autoFocus
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => e.key === "Escape" && onCancel()}
        className="h-7 w-56 font-mono text-[12px]"
      />
      <Button type="submit" size="xs" disabled={busy || !value}>
        Save
      </Button>
      <Button type="button" size="xs" variant="ghost" onClick={onCancel}>
        Cancel
      </Button>
    </form>
  );
}

function PlanFact({
  label,
  children,
  mono = false,
  wide = false,
}: {
  label: string;
  children: React.ReactNode;
  mono?: boolean;
  wide?: boolean;
}) {
  return (
    <div className={cn("min-w-0", wide && "col-span-2 sm:col-span-4")}>
      <dt className="text-[12px] text-subtle">{label}</dt>
      <dd className={cn("truncate", mono && "font-mono text-[12px]")} title={typeof children === "string" ? children : undefined}>
        {children}
      </dd>
    </div>
  );
}
