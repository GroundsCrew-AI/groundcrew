"use client";

import "@xyflow/react/dist/style.css";

import {
  Background,
  Controls,
  type EdgeTypes,
  MarkerType,
  type Node,
  type NodeTypes,
  ReactFlow,
} from "@xyflow/react";
import type { ELK as ElkApi } from "elkjs/lib/elk-api";
import { useTheme } from "next-themes";
import { useEffect, useMemo, useState } from "react";

import type { GraphEdge, GraphNode } from "@/lib/discovery";
import { edgeLabel, fromElk, groupOf, type Positioned, TOOLING_GROUP, TOOLING_STUB, toElk } from "@/lib/graph";

import { type GroupFlowNode, GroupNode, type ToolingStubFlowNode, ToolingStubNode } from "./group-node";
import { ResourceNode, type ResourceFlowNode } from "./resource-node";
import { RoutedEdge, type RoutedFlowEdge } from "./routed-edge";

// elkjs is large (about 1.4 MB): load it only when the map has something to lay out.
let elkPromise: Promise<ElkApi> | null = null;
function loadElk(): Promise<ElkApi> {
  elkPromise ??= import("elkjs/lib/elk.bundled.js").then((m) => new m.default());
  return elkPromise;
}
const nodeTypes: NodeTypes = { resource: ResourceNode, "group-box": GroupNode, "tooling-stub": ToolingStubNode };
const edgeTypes: EdgeTypes = { routed: RoutedEdge };

function useLayout(nodes: GraphNode[], edges: GraphEdge[], toolingOpen: boolean) {
  const signature = useMemo(
    () => `${nodes.map((n) => n.id).join(",")}|${edges.map((e) => e.id).join(",")}|${toolingOpen}`,
    [nodes, edges, toolingOpen],
  );
  const [state, setState] = useState<{ signature: string; layout: Positioned | null; error: string | null }>({
    signature: "",
    layout: null,
    error: null,
  });
  useEffect(() => {
    let cancelled = false;
    loadElk()
      .then((elk) => elk.layout(toElk(nodes, edges, toolingOpen)))
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
  const [toolingOpen, setToolingOpen] = useState(false);
  const { layout, error, ready, signature } = useLayout(nodes, edges, toolingOpen);
  const tooling = useMemo(() => nodes.filter((n) => groupOf(n).id === TOOLING_GROUP), [nodes]);
  const stubbed = useMemo(() => new Set(toolingOpen ? [] : tooling.map((n) => n.id)), [tooling, toolingOpen]);

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
      data: {
        label: group.label,
        provider: group.provider,
        count: group.id === TOOLING_GROUP ? tooling.length : (counts.get(group.id) ?? 0),
        onCollapse: group.id === TOOLING_GROUP && toolingOpen ? () => setToolingOpen(false) : undefined,
      },
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
            onOpen: onSelect,
          },
          draggable: false,
          // The button inside the node takes focus and opens the panel (click, Enter, Space).
          focusable: false,
        },
      ];
    });
    const stub = layout.nodes.get(TOOLING_STUB);
    const stubs: ToolingStubFlowNode[] = stub
      ? [
          {
            id: TOOLING_STUB,
            type: "tooling-stub",
            parentId: stub.groupId,
            position: { x: stub.x, y: stub.y },
            data: { count: tooling.length, names: tooling.map((n) => n.name), onExpand: () => setToolingOpen(true) },
            draggable: false,
            selectable: false,
          },
        ]
      : [];
    return [...groups, ...resources, ...stubs];
  }, [layout, nodes, selectedId, neighbors, tooling, toolingOpen, onSelect]);

  const flowEdges = useMemo<RoutedFlowEdge[]>(() => {
    const end = (id: string) => (stubbed.has(id) ? TOOLING_STUB : id);
    return edges
      .filter((e) => end(e.from_id) !== end(e.to_id))
      .map((e) => {
        const active = selectedId !== null && (e.from_id === selectedId || e.to_id === selectedId);
        const color = active ? "var(--primary)" : "var(--subtle)";
        return {
          id: e.id,
          type: "routed",
          source: end(e.from_id),
          target: end(e.to_id),
          zIndex: active ? 2 : 1,
          data: {
            route: layout?.edges.get(e.id) ?? null,
            label: edgeLabel(e.kind),
            active,
            faded: selectedId !== null && !active,
            dashed: e.confidence === "low",
          },
          markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color },
          ariaLabel: `${edgeLabel(e.kind)}, ${e.confidence} confidence`,
        };
      });
  }, [edges, selectedId, layout, stubbed]);

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
      edgeTypes={edgeTypes}
      colorMode={resolvedTheme === "light" ? "light" : "dark"}
      fitView
      fitViewOptions={{ padding: 0.06, maxZoom: 1 }}
      minZoom={0.15}
      maxZoom={1.75}
      nodesConnectable={false}
      nodesDraggable={false}
      onPaneClick={() => onSelect(null)}
      className="[--xy-background-color:transparent] [--xy-controls-button-background-color:var(--card)] [--xy-controls-button-border-color:var(--border)]"
    >
      <Background gap={22} size={1} color="var(--rail)" />
      <Controls showInteractive={false} position="bottom-left" />
    </ReactFlow>
  );
}
