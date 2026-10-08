import type { ElkExtendedEdge, ElkNode } from "elkjs/lib/elk-api";

import type { GraphEdge, GraphNode } from "@/lib/discovery";

export const PROVIDER_LABELS: Record<string, string> = {
  aws: "AWS",
  azure: "Azure",
  gcp: "GCP",
  github: "GitHub",
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
  uses: "uses",
  for: "for",
  pulls_from: "pulls image",
  logs_to: "logs to",
  built_from: "built from",
  deployed_by: "deployed by",
  assumes: "assumes",
  defined_in: "defined in",
  same_repo: "same repo",
  related: "related",
  runs_in: "runs in",
  runs_as: "runs as",
  routes_to: "routes to",
  deployment_of: "deployment of",
};

export function edgeLabel(kind: string): string {
  return EDGE_LABELS[kind] ?? kind.replaceAll("_", " ");
}

/** Network and identity plumbing: hidden unless asked for, to keep the map readable. */
export const INFRA_TYPES = new Set(["ec2:vpc", "ec2:security-group", "iam:role", "k8s:namespace"]);
/** Edges that point at where something is defined: shown in the side panel, not as lines. */
export const IAC_EDGE_KINDS = new Set(["defined_in"]);

export type MapFilters = {
  /** Providers to show; empty means all. */
  providers: string[];
  showInfra: boolean;
  showIac: boolean;
};

export const DEFAULT_FILTERS: MapFilters = { providers: [], showInfra: false, showIac: false };

export function visibleGraph(
  nodes: GraphNode[],
  edges: GraphEdge[],
  filters: MapFilters,
): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const shown = nodes.filter(
    (n) =>
      (filters.providers.length === 0 || filters.providers.includes(n.provider)) &&
      (filters.showInfra || !INFRA_TYPES.has(n.type)),
  );
  const ids = new Set(shown.map((n) => n.id));
  const keptEdges = edges.filter(
    (e) =>
      ids.has(e.from_id) && ids.has(e.to_id) && (filters.showIac || !IAC_EDGE_KINDS.has(e.kind)),
  );
  // With IaC links hidden, a repository only stays if something else points at it.
  const linked = new Set(keptEdges.flatMap((e) => [e.from_id, e.to_id]));
  const finalNodes = shown.filter((n) => n.type !== "github:repository" || filters.showIac || linked.has(n.id));
  return { nodes: finalNodes, edges: keptEdges };
}

export type Group = { id: string; label: string; provider: string };

/** Provider, then account, then region: "AWS 881050425883, us-east-2". */
export function groupOf(node: GraphNode): Group {
  const provider = PROVIDER_LABELS[node.provider] ?? node.provider;
  const parts = [node.account].filter(Boolean) as string[];
  if (node.provider === "aws" || node.provider === "azure") parts.push(node.region ?? "global");
  if (node.provider === "k8s" && node.region) parts.push(`namespace ${node.region}`);
  const id = ["group", node.provider, ...parts].join("|");
  return { id, label: [provider, ...parts].join(", ").replace(", ", " "), provider: node.provider };
}

export const NODE_WIDTH = 232;
export const NODE_HEIGHT = 66;

const ROOT_OPTIONS: Record<string, string> = {
  "elk.algorithm": "layered",
  "elk.direction": "RIGHT",
  "elk.hierarchyHandling": "INCLUDE_CHILDREN",
  "elk.layered.spacing.nodeNodeBetweenLayers": "90",
  "elk.spacing.nodeNode": "28",
  "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF",
  "elk.spacing.componentComponent": "60",
};

const GROUP_OPTIONS: Record<string, string> = {
  "elk.padding": "[top=40,left=20,bottom=20,right=20]",
};

/** The ELK input: one compound node per group, resources inside, edges at the root. */
export function toElk(nodes: GraphNode[], edges: GraphEdge[]): ElkNode {
  const groups = new Map<string, { group: Group; children: ElkNode[] }>();
  for (const node of nodes) {
    const group = groupOf(node);
    const entry = groups.get(group.id) ?? { group, children: [] };
    entry.children.push({ id: node.id, width: NODE_WIDTH, height: NODE_HEIGHT });
    groups.set(group.id, entry);
  }
  const elkEdges: ElkExtendedEdge[] = edges.map((e) => ({
    id: e.id,
    sources: [e.from_id],
    targets: [e.to_id],
  }));
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

export type Positioned = {
  groups: { group: Group; x: number; y: number; width: number; height: number }[];
  /** Resource positions relative to their group. */
  nodes: Map<string, { x: number; y: number; groupId: string }>;
};

export function fromElk(layout: ElkNode, nodes: GraphNode[]): Positioned {
  const groupById = new Map(nodes.map((n) => [groupOf(n).id, groupOf(n)]));
  const out: Positioned = { groups: [], nodes: new Map() };
  for (const g of layout.children ?? []) {
    const group = groupById.get(g.id);
    if (!group) continue;
    out.groups.push({ group, x: g.x ?? 0, y: g.y ?? 0, width: g.width ?? 0, height: g.height ?? 0 });
    for (const child of g.children ?? []) {
      out.nodes.set(child.id, { x: child.x ?? 0, y: child.y ?? 0, groupId: g.id });
    }
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
