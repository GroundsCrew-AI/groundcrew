import Link from "next/link";

import type { Run } from "@/lib/api/client";
import { agentName, elapsedMs, formatDuration, formatRelative, formatTimestamp, formatTokens } from "@/lib/format";

import { StatusPill } from "./badges";

/** Dense run rows: status, agent, goal, tokens, duration, when. */
export function RunList({ runs, now = Date.now() }: { runs: Run[]; now?: number }) {
  return (
    <ul className="divide-y divide-border">
      {runs.map((run) => {
        const duration = run.duration_ms ?? elapsedMs(run.started_at, run.finished_at, now);
        return (
          <li key={run.id}>
            <Link
              href={`/runs/${run.id}`}
              className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-4 gap-y-1 px-4 py-2.5 hover:bg-accent/60 focus-visible:bg-accent/60 sm:grid-cols-[116px_88px_minmax(0,1fr)_80px_64px_88px]"
            >
              <span>
                <StatusPill status={run.status} />
              </span>
              <span className="hidden truncate text-muted-foreground sm:block">{agentName(run.agent)}</span>
              <span className="truncate">{run.goal}</span>
              <span className="tabular hidden text-right font-mono text-[12px] text-muted-foreground sm:block">
                {formatTokens(run.total_tokens)}
              </span>
              <span className="tabular hidden text-right text-[12px] text-muted-foreground sm:block">
                {duration !== null ? formatDuration(duration) : ""}
              </span>
              <time
                dateTime={run.created_at}
                title={formatTimestamp(run.created_at)}
                className="text-right text-[12px] text-muted-foreground"
              >
                {formatRelative(run.created_at, now)}
              </time>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

export function RunListHeader() {
  return (
    <div
      aria-hidden="true"
      className="hidden grid-cols-[116px_88px_minmax(0,1fr)_80px_64px_88px] gap-x-4 border-b px-4 py-2 text-[11.5px] text-subtle sm:grid"
    >
      <span>Status</span>
      <span>Agent</span>
      <span>Goal</span>
      <span className="text-right">Tokens</span>
      <span className="text-right">Duration</span>
      <span className="text-right">Started</span>
    </div>
  );
}
