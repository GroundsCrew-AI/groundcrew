"use client";

import type { Node, NodeProps } from "@xyflow/react";
import { memo } from "react";

import { PROVIDER_MARKS } from "./resource-meta";

export type GroupNodeData = { label: string; provider: string; count: number };
export type GroupFlowNode = Node<GroupNodeData, "group-box">;

function GroupNodeView({ data }: NodeProps<GroupFlowNode>) {
  return (
    <div className="pointer-events-none h-full w-full rounded-xl border border-dashed bg-[color-mix(in_oklab,var(--muted)_55%,transparent)]">
      <div className="flex items-center gap-2 px-3 pt-2.5 text-[11.5px] text-muted-foreground">
        <span className="rounded border bg-card px-1 font-mono text-[10px]">
          {PROVIDER_MARKS[data.provider] ?? data.provider}
        </span>
        <span className="truncate font-medium">{data.label}</span>
        <span className="tabular text-subtle">{data.count}</span>
      </div>
    </div>
  );
}

export const GroupNode = memo(GroupNodeView);
