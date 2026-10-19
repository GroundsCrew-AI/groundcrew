"use client";

import { Handle, type Node, type NodeProps, Position } from "@xyflow/react";
import { cn } from "cn";
import { memo } from "react";

import type { GraphNode } from "@/lib/discovery";
import { typeLabel } from "@/lib/graph";

import { COVERAGE, ResourceIcon } from "./resource-meta";

export type ResourceNodeData = { resource: GraphNode; dimmed: boolean; selected: boolean };
export type ResourceFlowNode = Node<ResourceNodeData, "resource">;

function ResourceNodeView({ data }: NodeProps<ResourceFlowNode>) {
  const { resource, dimmed, selected } = data;
  const coverage = COVERAGE[resource.coverage] ?? COVERAGE.observed!;
  return (
    <div
      className={cn(
        "flex h-[84px] w-[288px] items-center gap-3 rounded-lg border bg-card px-3 text-left shadow-[0_1px_0_rgb(0_0_0/0.03)] transition-[opacity,box-shadow,border-color] duration-150",
        selected && "border-primary ring-2 ring-primary/25",
        dimmed && "opacity-35",
        resource.stale && "border-dashed opacity-60",
      )}
    >
      <Handle type="target" position={Position.Left} className="!size-1.5 !border-0 !bg-transparent" />
      <span className="flex size-10 shrink-0 items-center justify-center rounded-md border bg-background text-muted-foreground">
        <ResourceIcon type={resource.type} className="size-5" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <span className="truncate text-[14px] font-medium" title={resource.name}>
            {resource.name}
          </span>
          <span
            aria-label={`${coverage.label} coverage`}
            title={`${coverage.label}: ${coverage.hint}`}
            className="size-2 shrink-0 rounded-full"
            style={{ background: coverage.color }}
          />
        </span>
        <span className="block truncate text-[12.5px] text-muted-foreground">
          {typeLabel(resource.type)}
          {resource.stale && " (not seen in the last discovery)"}
        </span>
        {resource.facts.length > 0 && (
          <span className="block truncate font-mono text-[12px] text-subtle">{resource.facts.join("  ")}</span>
        )}
      </span>
      <Handle type="source" position={Position.Right} className="!size-1.5 !border-0 !bg-transparent" />
    </div>
  );
}

export const ResourceNode = memo(ResourceNodeView);
