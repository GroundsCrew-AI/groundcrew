import type { Connection, GraphEdge, GraphNode, ResourceDetail } from "@/lib/discovery";

export function node(id: string, overrides: Partial<GraphNode> = {}): GraphNode {
  return {
    id,
    connection_id: "conn-aws",
    provider: "aws",
    type: "ecs:service",
    name: id,
    account: "881050425883",
    region: "us-east-2",
    coverage: "observed",
    role: "app",
    stale: false,
    iac_path: null,
    iac_url: null,
    repo: null,
    facts: [],
    last_seen_at: "2026-10-08T09:00:00Z",
    ...overrides,
  };
}

export function edge(id: string, from: string, to: string, overrides: Partial<GraphEdge> = {}): GraphEdge {
  return { id, from_id: from, to_id: to, kind: "calls", confidence: "high", evidence: "because", rule: "x", ...overrides };
}

export function connection(overrides: Partial<Connection> = {}): Connection {
  return {
    id: "conn-aws",
    workspace_id: "ws-1",
    kind: "aws",
    name: "AWS 881050425883",
    status: "connected",
    preview: false,
    account: "881050425883",
    role_arn: "arn:aws:iam::881050425883:role/groundcrew-read-only",
    external_id: "gc-abc",
    repos: [],
    team_id: null,
    resource_group: null,
    server: null,
    tier: "observe",
    deploy_role_arn: null,
    regions: ["us-east-2"],
    regions_chosen: false,
    error: null,
    resource_count: 21,
    last_discovery: null,
    created_at: "2026-10-08T09:00:00Z",
    ...overrides,
  };
}

export function detail(overrides: Partial<ResourceDetail> = {}): ResourceDetail {
  return {
    ...node("api", { name: "checkout-api", coverage: "diagnosable", iac_path: "infra/aws/ecs.tf#L94",
      iac_url: "https://github.com/GroundsCrew-AI/checkout-demo/blob/HEAD/infra/aws/ecs.tf#L94",
      repo: "GroundsCrew-AI/checkout-demo" }),
    tags: {},
    config: {
      running_count: 1,
      desired_count: 1,
      task_family: "checkout-api",
      task_revision: 7,
      public_ips: ["3.15.10.20"],
      has_load_balancer: false,
      containers: [
        {
          image_repository: "checkout-api",
          image_tag: "cef79e7a1b2c3d4e",
          ports: [{ port: 8000 }],
          secrets: [{ name: "DATABASE_URL", value_from: "arn:aws:secretsmanager:x" }],
          env_names: ["LOG_LEVEL"],
          log: { group: "/ecs/checkout-api" },
        },
      ],
    },
    edges: [
      {
        ...edge("e1", "api", "db", { kind: "reads", evidence: "Security group checkout-db allows port 5432 from checkout-tasks." }),
        direction: "out",
        other: { id: "db", name: "checkout-orders-db", type: "rds:db", provider: "aws" },
      },
      {
        ...edge("e2", "web", "api", { kind: "calls", confidence: "medium", evidence: "Vercel env API_URL (encrypted, value not read)." }),
        direction: "in",
        other: { id: "web", name: "checkout-demo", type: "vercel:project", provider: "vercel" },
      },
    ],
    recent_deploys: [
      { at: "2026-10-08T08:31:00Z", source: "GitHub Actions deploy (deploy.yml)", status: "success", title: "deploy", ref: "cef79e7", url: "https://github.com/x/actions/runs/901" },
    ],
    ...overrides,
  };
}
