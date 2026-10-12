"use client";

import { cn } from "cn";
import { ChevronRight } from "lucide-react";
import { useState } from "react";

import { formatTimestamp } from "@/lib/format";
import { type EvidenceItem, evidenceById, type RootCause } from "@/lib/incidents";

const CHANGE: Record<string, string> = {
  commit: "Commit",
  secret_change: "Secret change",
  deploy: "Deploy",
  config: "Config change",
  other: "Change",
};

/** The root cause, with every claim linked to the evidence lines it cites. */
export function RootCausePanel({ rootCause, evidence }: { rootCause: RootCause; evidence: EvidenceItem[] }) {
  const byId = evidenceById(evidence);
  const change = rootCause.triggering_change;
  const claims = rootCause.claims ?? [];
  return (
    <section aria-label="Root cause" className="space-y-3 rounded-lg border bg-card px-4 py-3">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <h2 className="text-[13px] font-medium">Root cause</h2>
        <span className="text-[12px] text-muted-foreground">confidence {rootCause.confidence}</span>
      </div>
      <p className="text-[14px]">{rootCause.summary}</p>
      <p className="text-[13px] text-muted-foreground">{rootCause.root_cause}</p>
      {change && (
        <p className="text-[12.5px]">
          <span className="text-muted-foreground">Started by:</span> {CHANGE[change.kind] ?? change.kind}{" "}
          {change.ref && <span className="font-mono">{change.ref.length > 48 ? `${change.ref.slice(0, 48)}...` : change.ref}</span>}
          {change.at && <span className="text-muted-foreground"> at {formatTimestamp(change.at)}</span>}
          {change.description && <span className="text-muted-foreground">: {change.description}</span>}
        </p>
      )}
      {claims.length > 0 && (
        <ul className="space-y-1.5">
          {claims.map((claim, i) => (
            <Claim key={i} text={claim.text} lines={claim.evidence_ids.map((id) => byId.get(id)).filter(Boolean) as EvidenceItem[]} />
          ))}
        </ul>
      )}
    </section>
  );
}

function Claim({ text, lines }: { text: string; lines: EvidenceItem[] }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="text-[13px]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex items-start gap-1 text-left hover:text-foreground"
      >
        <ChevronRight aria-hidden="true" className={cn("mt-0.5 size-3.5 shrink-0 transition-transform", open && "rotate-90")} />
        <span>
          {text} <span className="text-[12px] text-subtle">{lines.length} {lines.length === 1 ? "line" : "lines"}</span>
        </span>
      </button>
      {open && (
        <ul className="mt-1 ml-5 space-y-0.5 rounded-md border bg-background px-3 py-2 font-mono text-[11.5px]">
          {lines.map((line) => (
            <li key={line.id} className="break-words">
              <span className="text-subtle">{formatTimestamp(line.timestamp)}</span>{" "}
              <span className="text-muted-foreground">{line.source}</span> {line.line}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}
