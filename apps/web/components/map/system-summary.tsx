import { TriangleAlert } from "lucide-react";
import Link from "next/link";

import type { SystemSummary as Summary } from "@/lib/discovery";
import { formatRelative } from "@/lib/format";
import { isRiskLine, stripRisk, summaryLines } from "@/lib/graph";

/** Scout's summary of the system, with the risks the graph rules found. */
export function SystemSummary({ summary }: { summary: Summary | null | undefined }) {
  if (!summary) return null;
  const lines = summaryLines(summary.text);
  const facts = lines.filter((l) => !isRiskLine(l));
  // Risks from the rules are the source of truth; the model's risk lines are its wording.
  const risks = summary.risks.length > 0 ? summary.risks.map(stripRisk) : lines.filter(isRiskLine).map(stripRisk);
  return (
    <section aria-labelledby="summary-title" className="rounded-lg border bg-card px-4 py-3.5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="summary-title" className="text-[13px] font-medium">
          System summary
        </h2>
        <p className="text-[12px] text-subtle">
          {summary.generated_at ? `Scout, Nemotron Nano, ${formatRelative(summary.generated_at)}` : "Not written yet"}
          {summary.run_id && (
            <>
              {" "}
              <Link href={`/runs/${summary.run_id}`} className="underline-offset-2 hover:underline">
                View run
              </Link>
            </>
          )}
        </p>
      </div>
      {facts.length > 0 ? (
        <ul className="mt-2 grid gap-x-8 gap-y-1 md:grid-cols-2">
          {facts.map((line) => (
            <li key={line} className="flex gap-2 text-[12.5px] leading-relaxed">
              <span aria-hidden="true" className="mt-[8px] size-1 shrink-0 rounded-full bg-subtle" />
              {line}
            </li>
          ))}
        </ul>
      ) : (
        summary.error && (
          <p className="mt-2 text-[12.5px] text-muted-foreground">The summary could not be written: {summary.error}</p>
        )
      )}
      {risks.length > 0 && (
        <ul aria-label="Risks" className="mt-3 space-y-1 border-t pt-3">
          {risks.map((risk) => (
            <li key={risk} className="flex gap-2 text-[12.5px] leading-relaxed">
              <TriangleAlert aria-hidden="true" className="mt-[3px] size-3.5 shrink-0 text-[var(--status-waiting)]" />
              <span>{risk}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
