"use client";

import { Check, Copy } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import type { RunDetail, Step } from "@/lib/api/client";
import { agentName, formatTimestamp } from "@/lib/format";

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1.5">
      <dt className="shrink-0 text-muted-foreground">{label}</dt>
      <dd className="min-w-0 truncate text-right">{children}</dd>
    </div>
  );
}

/** Reference facts for a run, beside the timeline on wide screens. */
export function RunDetails({ run, steps }: { run: RunDetail; steps: Step[] }) {
  const [copied, setCopied] = useState(false);
  const modelCalls = steps.filter((s) => s.kind === "llm").length;
  const tools = new Set(steps.filter((s) => s.kind === "tool").map((s) => s.tool_name)).size;
  const models = [...new Set(steps.map((s) => s.model_id).filter(Boolean))];

  async function copyId() {
    try {
      await navigator.clipboard.writeText(run.id);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard blocked: the ID is still selectable.
    }
  }

  return (
    <aside aria-label="Run details" className="text-[12.5px]">
      <dl className="divide-y">
        <div className="flex items-center justify-between gap-2 pb-2">
          <dt className="text-muted-foreground">Run ID</dt>
          <dd className="flex min-w-0 items-center gap-1">
            <span className="truncate font-mono text-[12px]" title={run.id}>
              {run.id.slice(0, 8)}
            </span>
            <Button variant="ghost" size="icon-xs" onClick={copyId} aria-label="Copy run ID">
              {copied ? <Check /> : <Copy />}
            </Button>
          </dd>
        </div>
        <Row label="Agent">{agentName(run.agent)}</Row>
        <Row label="Created">{formatTimestamp(run.created_at)}</Row>
        {run.finished_at && <Row label="Finished">{formatTimestamp(run.finished_at)}</Row>}
        <Row label="Model calls">{modelCalls}</Row>
        <Row label="Tools used">{tools}</Row>
        <Row label="Prompt tokens">
          <span className="font-mono">{run.prompt_tokens.toLocaleString("en-US")}</span>
        </Row>
        <Row label="Completion tokens">
          <span className="font-mono">{run.completion_tokens.toLocaleString("en-US")}</span>
        </Row>
      </dl>
      {models.length > 0 && (
        <div className="mt-4">
          <p className="text-muted-foreground">Models</p>
          <ul className="mt-1.5 space-y-1">
            {models.map((model) => (
              <li key={model} className="truncate font-mono text-[12px]" title={model ?? undefined}>
                {model}
              </li>
            ))}
          </ul>
        </div>
      )}
    </aside>
  );
}
