"use client";

import { Handle, type Node, type NodeProps, Position } from "@xyflow/react";
import { ChevronDown, ChevronRight, Wrench } from "lucide-react";
import { memo } from "react";

import { PROVIDER_MARKS } from "./resource-meta";

export type GroupNodeData = {
  label: string;
  provider: string;
  count: number;
  /** Tooling only: collapse it again. */
  onCollapse?: () => void;
};
export type GroupFlowNode = Node<GroupNodeData, "group-box">;

function GroupNodeView({ data }: NodeProps<GroupFlowNode>) {
  return (
    <div className="pointer-events-none h-full w-full rounded-xl border border-dashed bg-[color-mix(in_oklab,var(--muted)_55%,transparent)]">
      <div className="flex items-center gap-2 px-3.5 pt-3 text-[13px] text-muted-foreground">
        <span className="rounded border bg-card px-1.5 font-mono text-[11px]">
          {PROVIDER_MARKS[data.provider] ?? data.provider}
        </span>
        <span className="truncate font-medium">{data.label}</span>
        <span className="tabular text-subtle">{data.count}</span>
        {data.onCollapse && (
          <button
            type="button"
            onClick={data.onCollapse}
            className="pointer-events-auto ml-auto inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[12px] hover:bg-accent"
          >
            <ChevronDown aria-hidden="true" className="size-3.5" />
            Collapse
          </button>
        )}
      </div>
    </div>
  );
}

export const GroupNode = memo(GroupNodeView);

export type ToolingStubData = { count: number; names: string[]; onExpand: () => void };
export type ToolingStubFlowNode = Node<ToolingStubData, "tooling-stub">;

/** The collapsed Tooling group: state buckets, template buckets, Groundcrew's own role. */
function ToolingStubView({ data }: NodeProps<ToolingStubFlowNode>) {
  return (
    <button
      type="button"
      onClick={data.onExpand}
      title={data.names.join(", ")}
      className="flex h-[64px] w-[288px] items-center gap-3 rounded-lg border border-dashed bg-card px-3 text-left hover:border-primary"
    >
      <Handle type="target" position={Position.Left} className="!size-1.5 !border-0 !bg-transparent" />
      <span className="flex size-10 shrink-0 items-center justify-center rounded-md border bg-background text-muted-foreground">
        <Wrench aria-hidden="true" className="size-5" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-[14px] font-medium">
          {data.count} tooling {data.count === 1 ? "resource" : "resources"}
        </span>
        <span className="block truncate text-[12.5px] text-muted-foreground">Not used by the app. Show them</span>
      </span>
      <ChevronRight aria-hidden="true" className="size-4 text-muted-foreground" />
      <Handle type="source" position={Position.Right} className="!size-1.5 !border-0 !bg-transparent" />
    </button>
  );
}

export const ToolingStubNode = memo(ToolingStubView);
