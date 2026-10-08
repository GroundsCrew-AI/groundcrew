"use client";

import "@xyflow/react/dist/style.css";

import {
  Background,
  Controls,
  type Edge,
  MarkerType,
  type Node,
  type NodeTypes,
  ReactFlow,
} from "@xyflow/react";
import type { ELK as ElkApi } from "elkjs/lib/elk-api";
import { useTheme } from "next-themes";
import { useEffect, useMemo, useState } from "react";

import type { GraphEdge, GraphNode } from "@/lib/discovery";
import { edgeLabel, fromElk, type Positioned, toElk } from "@/lib/graph";

import { type GroupFlowNode, GroupNode } from "./group-node";
import { ResourceNode, type ResourceFlowNode } from "./resource-node";

// elkjs is large (about 1.4 MB): load it only when the map has something to lay out.
let elkPromise: Promise<ElkApi> | null = null;
function loadElk(): Promise<ElkApi> {
  elkPromise ??= import("elkjs/lib/elk.bundled.js").then((m) => new m.default());
  return elkPromise;
}
const nodeTypes: NodeTypes = { resource: ResourceNode, "group-box": GroupNode };

function useLayout(nodes: GraphNode[], edges: GraphEdge[]) {
  const signature = useMemo(
    () => `${nodes.map((n) => n.id).join(",")}|${edges.map((e) => e.id).join(",")}`,
    [nodes, edges],
  );
  const [state, setState] = useState<{ signature: string; layout: Positioned | null; error: string | null }>({
    signature: "",
    layout: null,
    error: null,
  });
  useEffect(() => {
    let cancelled = false;
    loadElk()
      .then((elk) => elk.layout(toElk(nodes, edges)))
      .then((result) => {
        if (!cancelled) setState({ signature, layout: fromElk(result, nodes), error: null });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          const message = error instanceof Error ? error.message : "Layout failed.";
          setState({ signature, layout: null, error: message });
        }
      });
    return () => {
      cancelled = true;
    };
    // The signature covers nodes and edges; re-running on new array identities is wasted work.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);
  return { ...state, ready: state.signature === signature, signature };
}

export function MapCanvas({
  nodes,
  edges,
  selectedId,
  onSelect,
}: {
  nodes: GraphNode[];
  edges: GraphEdge[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
}) {
  const { resolvedTheme } = useTheme();
  const { layout, error, ready, signature } = useLayout(nodes, edges);

  const neighbors = useMemo(() => {
    if (!selectedId) return null;
    const ids = new Set([selectedId]);
    for (const e of edges) {
      if (e.from_id === selectedId) ids.add(e.to_id);
      if (e.to_id === selectedId) ids.add(e.from_id);
    }
    return ids;
  }, [selectedId, edges]);

  const flowNodes = useMemo<Node[]>(() => {
    if (!layout) return [];
    const counts = new Map<string, number>();
    for (const p of layout.nodes.values()) counts.set(p.groupId, (counts.get(p.groupId) ?? 0) + 1);
    const groups: GroupFlowNode[] = layout.groups.map(({ group, x, y, width, height }) => ({
      id: group.id,
      type: "group-box",
      position: { x, y },
      data: { label: group.label, provider: group.provider, count: counts.get(group.id) ?? 0 },
      style: { width, height },
      selectable: false,
      draggable: false,
      zIndex: -1,
    }));
    const resources: ResourceFlowNode[] = nodes.flatMap((resource) => {
      const position = layout.nodes.get(resource.id);
      if (!position) return [];
      return [
        {
          id: resource.id,
          type: "resource",
          parentId: position.groupId,
          position: { x: position.x, y: position.y },
          data: {
            resource,
            selected: resource.id === selectedId,
            dimmed: neighbors !== null && !neighbors.has(resource.id),
          },
          draggable: false,
        },
      ];
    });
    return [...groups, ...resources];
  }, [layout, nodes, selectedId, neighbors]);

  const flowEdges = useMemo<Edge[]>(
    () =>
      edges.map((e) => {
        const active = selectedId !== null && (e.from_id === selectedId || e.to_id === selectedId);
        const faded = selectedId !== null && !active;
        const color = active ? "var(--primary)" : "var(--subtle)";
        return {
          id: e.id,
          source: e.from_id,
          target: e.to_id,
          label: edgeLabel(e.kind),
          zIndex: active ? 2 : 1,
          style: {
            stroke: color,
            strokeWidth: active ? 1.75 : 1.25,
            strokeDasharray: e.confidence === "low" ? "4 4" : undefined,
            opacity: faded ? 0.18 : 1,
          },
          labelStyle: { fontSize: 10.5, fill: active ? "var(--foreground)" : "var(--muted-foreground)" },
          labelBgStyle: { fill: "var(--background)", fillOpacity: faded ? 0.4 : 0.92 },
          labelBgPadding: [4, 2] as [number, number],
          labelBgBorderRadius: 4,
          markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color },
          ariaLabel: `${edgeLabel(e.kind)}, ${e.confidence} confidence`,
        };
      }),
    [edges, selectedId],
  );

  if (error) {
    return (
      <p role="alert" className="p-6 text-muted-foreground">
        The map could not be laid out: {error}
      </p>
    );
  }
  if (!ready) {
    return <p className="p-6 text-muted-foreground">Laying out the map</p>;
  }
  return (
    <ReactFlow
      key={signature}
      nodes={flowNodes}
      edges={flowEdges}
      nodeTypes={nodeTypes}
      colorMode={resolvedTheme === "light" ? "light" : "dark"}
      fitView
      fitViewOptions={{ padding: 0.12 }}
      minZoom={0.15}
      maxZoom={1.75}
      nodesConnectable={false}
      nodesDraggable={false}
      onNodeClick={(_event, node) => node.type === "resource" && onSelect(node.id)}
      onPaneClick={() => onSelect(null)}
      className="[--xy-background-color:transparent] [--xy-controls-button-background-color:var(--card)] [--xy-controls-button-border-color:var(--border)]"
    >
      <Background gap={22} size={1} color="var(--rail)" />
      <Controls showInteractive={false} position="bottom-left" />
    </ReactFlow>
  );
}
