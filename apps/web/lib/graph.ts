import type { ElkExtendedEdge, ElkNode } from "elkjs/lib/elk-api";

import type { GraphEdge, GraphNode } from "@/lib/discovery";

export const PROVIDER_LABELS: Record<string, string> = {
  aws: "AWS",
  azure: "Azure",
  gcp: "GCP",
  github: "GitHub",
  groundcrew: "Groundcrew",
  k8s: "Kubernetes",
  vercel: "Vercel",
  vm: "VM",
};

const TYPE_LABELS: Record<string, string> = {
  "ecs:cluster": "ECS cluster",
  "ecs:service": "ECS service",
  "ecr:repository": "ECR repository",
  "rds:db": "RDS database",
  "rds:cluster": "RDS cluster",
  "secretsmanager:secret": "Secret (metadata)",
  "logs:log-group": "Log group",
  "iam:role": "IAM role",
  "ec2:security-group": "Security group",
  "ec2:vpc": "VPC",
  "ec2:instance": "EC2 instance",
  "lambda:function": "Lambda function",
  "s3:bucket": "S3 bucket",
  "sqs:queue": "SQS queue",
  "sns:topic": "SNS topic",
  "dynamodb:table": "DynamoDB table",
  "elasticloadbalancing:loadbalancer/app": "Load balancer",
  "github:repository": "Repository",
  "github:workflow": "Workflow",
  "vercel:project": "Vercel project",
  "vercel:deployment": "Production deployment",
  "groundcrew:deployer": "Groundcrew deploys",
  "k8s:namespace": "Namespace",
  "k8s:deployment": "Deployment",
  "k8s:statefulset": "StatefulSet",
  "k8s:service": "Service",
  "k8s:ingress": "Ingress",
  "microsoft.web/sites": "App Service",
  "microsoft.web/serverfarms": "App Service plan",
  "microsoft.containerregistry/registries": "Container registry",
  "microsoft.dbforpostgresql/flexibleservers": "PostgreSQL server",
  "microsoft.keyvault/vaults": "Key Vault",
};

export function typeLabel(type: string): string {
  return TYPE_LABELS[type] ?? type.split(/[:/]/).pop()!.replaceAll("-", " ");
}

export const EDGE_LABELS: Record<string, string> = {
  calls: "calls",
  reads: "reads",
  deployed_by: "deployed by",
  defined_in: "defined in",
  uses: "uses",
  pulls_from: "pulls image",
  logs_to: "logs to",
};

export function edgeLabel(kind: string): string {
  return EDGE_LABELS[kind] ?? kind.replaceAll("_", " ");
}

/** Edges that point at where something is defined: shown in the side panel, not as lines. */
export const IAC_EDGE_KINDS = new Set(["defined_in"]);

export type MapFilters = {
  /** Providers to show; empty means all. */
  providers: string[];
  /** Draw "defined in" lines to repositories (there is one per Terraform resource). */
  showIac: boolean;
};

export const DEFAULT_FILTERS: MapFilters = { providers: [], showIac: false };

/** Every resource of the chosen providers; IaC lines only when asked for. */
export function visibleGraph(
  nodes: GraphNode[],
  edges: GraphEdge[],
  filters: MapFilters,
): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const shown = nodes.filter((n) => filters.providers.length === 0 || filters.providers.includes(n.provider));
  const ids = new Set(shown.map((n) => n.id));
  const keptEdges = edges.filter(
    (e) => ids.has(e.from_id) && ids.has(e.to_id) && (filters.showIac || !IAC_EDGE_KINDS.has(e.kind)),
  );
  return { nodes: shown, edges: keptEdges };
}

export type Group = { id: string; label: string; provider: string };

export const TOOLING_GROUP = "group|tooling";
/** Stands in for the tooling resources while their group is collapsed. */
export const TOOLING_STUB = "tooling-stub";

/** Provider, then account, then region: "AWS 881050425883, us-east-2". Tooling has its own group. */
export function groupOf(node: GraphNode): Group {
  if (node.role === "tooling") return { id: TOOLING_GROUP, label: "Tooling", provider: "tooling" };
  const provider = PROVIDER_LABELS[node.provider] ?? node.provider;
  const parts = [node.account].filter(Boolean) as string[];
  if (node.provider === "aws" || node.provider === "azure") parts.push(node.region ?? "global");
  if (node.provider === "k8s" && node.region) parts.push(`namespace ${node.region}`);
  const id = ["group", node.provider, ...parts].join("|");
  return { id, label: [provider, ...parts].join(", ").replace(", ", " "), provider: node.provider };
}

export const NODE_WIDTH = 288;
export const NODE_HEIGHT = 84;
export const STUB_HEIGHT = 64;
const LABEL_HEIGHT = 18;

/** Width of an edge label at the 12px label font (about 6.6px a character, plus padding). */
export function labelWidth(text: string): number {
  return Math.ceil(text.length * 6.6) + 14;
}

const ROOT_OPTIONS: Record<string, string> = {
  "elk.algorithm": "layered",
  "elk.direction": "RIGHT",
  "elk.hierarchyHandling": "INCLUDE_CHILDREN",
  // Edge routes and label positions in root coordinates, the same frame as React Flow.
  "elk.json.edgeCoords": "ROOT",
  "elk.edgeLabels.placement": "CENTER",
  "elk.edgeRouting": "ORTHOGONAL",
  "elk.layered.spacing.nodeNodeBetweenLayers": "120",
  "elk.layered.spacing.edgeNodeBetweenLayers": "28",
  "elk.layered.spacing.edgeEdgeBetweenLayers": "14",
  "elk.spacing.nodeNode": "28",
  "elk.spacing.edgeLabel": "6",
  "elk.spacing.edgeEdge": "14",
  "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
  "elk.spacing.componentComponent": "80",
};

const GROUP_OPTIONS: Record<string, string> = {
  "elk.padding": "[top=48,left=24,bottom=24,right=24]",
};

/**
 * The ELK input: one compound node per group, resources inside, edges at the root with sized
 * labels so ELK keeps them apart. A collapsed Tooling group has one stub node instead of its
 * resources, and their edges end at the stub.
 */
export function toElk(nodes: GraphNode[], edges: GraphEdge[], toolingOpen = false): ElkNode {
  const groups = new Map<string, { group: Group; children: ElkNode[] }>();
  const stubbed = new Set<string>();
  for (const node of nodes) {
    const group = groupOf(node);
    const entry = groups.get(group.id) ?? { group, children: [] };
    if (group.id === TOOLING_GROUP && !toolingOpen) {
      stubbed.add(node.id);
      if (entry.children.length === 0) entry.children.push({ id: TOOLING_STUB, width: NODE_WIDTH, height: STUB_HEIGHT });
    } else {
      entry.children.push({ id: node.id, width: NODE_WIDTH, height: NODE_HEIGHT });
    }
    groups.set(group.id, entry);
  }
  const end = (id: string) => (stubbed.has(id) ? TOOLING_STUB : id);
  const elkEdges: ElkExtendedEdge[] = edges
    .filter((e) => end(e.from_id) !== end(e.to_id))
    .map((e) => {
      const text = edgeLabel(e.kind);
      return {
        id: e.id,
        sources: [end(e.from_id)],
        targets: [end(e.to_id)],
        labels: [{ id: `${e.id}:label`, text, width: labelWidth(text), height: LABEL_HEIGHT }],
      };
    });
  return {
    id: "root",
    layoutOptions: ROOT_OPTIONS,
    children: [...groups.values()].map(({ group, children }) => ({
      id: group.id,
      layoutOptions: GROUP_OPTIONS,
      children,
    })),
    edges: elkEdges,
  };
}

export type Point = { x: number; y: number };
export type EdgeRoute = { points: Point[]; label: Point | null };

export type Positioned = {
  groups: { group: Group; x: number; y: number; width: number; height: number }[];
  /** Resource positions relative to their group. */
  nodes: Map<string, { x: number; y: number; groupId: string }>;
  /** Edge routes and label centers in root coordinates. */
  edges: Map<string, EdgeRoute>;
};

export function fromElk(layout: ElkNode, nodes: GraphNode[]): Positioned {
  const groupById = new Map(nodes.map((n) => [groupOf(n).id, groupOf(n)]));
  const out: Positioned = { groups: [], nodes: new Map(), edges: new Map() };
  for (const g of layout.children ?? []) {
    const group = groupById.get(g.id);
    if (!group) continue;
    out.groups.push({ group, x: g.x ?? 0, y: g.y ?? 0, width: g.width ?? 0, height: g.height ?? 0 });
    for (const child of g.children ?? []) {
      out.nodes.set(child.id, { x: child.x ?? 0, y: child.y ?? 0, groupId: g.id });
    }
  }
  for (const e of (layout.edges ?? []) as ElkExtendedEdge[]) {
    const section = e.sections?.[0];
    if (!section) continue;
    const label = e.labels?.[0];
    out.edges.set(e.id, {
      points: [section.startPoint, ...(section.bendPoints ?? []), section.endPoint],
      label:
        label && label.x !== undefined && label.y !== undefined
          ? { x: label.x + (label.width ?? 0) / 2, y: label.y + (label.height ?? 0) / 2 }
          : null,
    });
  }
  return out;
}

/** Bullet lines of a model summary, without their markers. */
export function summaryLines(text: string | null | undefined): string[] {
  if (!text) return [];
  return text
    .split("\n")
    .map((line) => line.replace(/^\s*(?:[-*•]|\d+[.)])\s+/, "").trim())
    .filter(Boolean);
}

export function isRiskLine(line: string): boolean {
  return /^risk\b/i.test(line);
}

/** "Risk: x" -> "x" */
export function stripRisk(line: string): string {
  return line.replace(/^risk\s*:\s*/i, "");
}
