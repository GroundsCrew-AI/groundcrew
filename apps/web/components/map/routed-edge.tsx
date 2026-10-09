"use client";

import { BaseEdge, type Edge, EdgeLabelRenderer, type EdgeProps, getSmoothStepPath } from "@xyflow/react";
import { cn } from "cn";
import { memo } from "react";

import type { EdgeRoute, Point } from "@/lib/graph";

export type RoutedEdgeData = {
  route: EdgeRoute | null;
  label: string;
  active: boolean;
  faded: boolean;
  dashed: boolean;
};
export type RoutedFlowEdge = Edge<RoutedEdgeData, "routed">;

const RADIUS = 8;

/** An orthogonal polyline with rounded corners through ELK's points. */
export function roundedPath(points: Point[]): string {
  if (points.length < 2) return "";
  let d = `M ${points[0]!.x} ${points[0]!.y}`;
  for (let i = 1; i < points.length - 1; i++) {
    const prev = points[i - 1]!;
    const here = points[i]!;
    const next = points[i + 1]!;
    const r = Math.min(RADIUS, Math.hypot(here.x - prev.x, here.y - prev.y) / 2, Math.hypot(next.x - here.x, next.y - here.y) / 2);
    const inX = here.x - Math.sign(here.x - prev.x) * r;
    const inY = here.y - Math.sign(here.y - prev.y) * r;
    const outX = here.x + Math.sign(next.x - here.x) * r;
    const outY = here.y + Math.sign(next.y - here.y) * r;
    d += ` L ${inX} ${inY} Q ${here.x} ${here.y} ${outX} ${outY}`;
  }
  const last = points[points.length - 1]!;
  return `${d} L ${last.x} ${last.y}`;
}

/** Draws the route and label ELK computed, so labels never sit on top of each other. */
function RoutedEdgeView(props: EdgeProps<RoutedFlowEdge>) {
  const { id, data, markerEnd } = props;
  const route = data?.route;
  const [fallbackPath, fallbackX, fallbackY] = getSmoothStepPath(props);
  const path = route ? roundedPath(route.points) : fallbackPath;
  const at = route?.label ?? { x: fallbackX, y: fallbackY };
  const color = data?.active ? "var(--primary)" : "var(--subtle)";
  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        markerEnd={markerEnd}
        style={{
          stroke: color,
          strokeWidth: data?.active ? 2 : 1.5,
          strokeDasharray: data?.dashed ? "5 5" : undefined,
          opacity: data?.faded ? 0.18 : 1,
        }}
      />
      <EdgeLabelRenderer>
        <span
          style={{ transform: `translate(-50%, -50%) translate(${at.x}px, ${at.y}px)` }}
          className={cn(
            "nodrag nopan pointer-events-none absolute whitespace-nowrap rounded bg-background/95 px-1.5 py-px text-[12px] leading-4",
            data?.active ? "font-medium text-foreground" : "text-muted-foreground",
            data?.faded && "opacity-30",
          )}
        >
          {data?.label}
        </span>
      </EdgeLabelRenderer>
    </>
  );
}

export const RoutedEdge = memo(RoutedEdgeView);
