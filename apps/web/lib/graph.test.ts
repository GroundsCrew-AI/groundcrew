import { describe, expect, it } from "vitest";

import { edge, node } from "@/tests/discovery-fixtures";

import {
  DEFAULT_FILTERS,
  edgeLabel,
  fromElk,
  groupOf,
  isRiskLine,
  stripRisk,
  summaryLines,
  toElk,
  typeLabel,
  visibleGraph,
} from "./graph";

describe("graph helpers", () => {
  it("groups by provider, account, and region", () => {
    expect(groupOf(node("a")).label).toBe("AWS 881050425883, us-east-2");
    expect(groupOf(node("r", { type: "iam:role", region: null })).label).toBe("AWS 881050425883, global");
    expect(groupOf(node("v", { provider: "vercel", account: "ground-crew1", region: null })).label).toBe("Vercel ground-crew1");
  });

  it("hides network, IAM, and IaC links by default", () => {
    const nodes = [
      node("api"),
      node("sg", { type: "ec2:security-group" }),
      node("repo", { provider: "github", type: "github:repository", region: null }),
      node("wf", { provider: "github", type: "github:workflow", region: null }),
    ];
    const edges = [
      edge("e1", "api", "sg", { kind: "uses" }),
      edge("e2", "api", "repo", { kind: "defined_in" }),
      edge("e3", "api", "wf", { kind: "deployed_by" }),
    ];
    const shown = visibleGraph(nodes, edges, DEFAULT_FILTERS);
    expect(shown.nodes.map((n) => n.id)).toEqual(["api", "wf"]);
    expect(shown.edges.map((e) => e.id)).toEqual(["e3"]);
    const all = visibleGraph(nodes, edges, { providers: [], showInfra: true, showIac: true });
    expect(all.nodes).toHaveLength(4);
    expect(all.edges).toHaveLength(3);
    const awsOnly = visibleGraph(nodes, edges, { ...DEFAULT_FILTERS, providers: ["aws"] });
    expect(awsOnly.nodes.map((n) => n.id)).toEqual(["api"]);
  });

  it("builds a compound ELK graph and reads positions back", () => {
    const nodes = [node("api"), node("web", { provider: "vercel", account: "t", region: null })];
    const elk = toElk(nodes, [edge("e", "web", "api")]);
    expect(elk.children?.map((c) => c.children?.map((n) => n.id))).toEqual([["api"], ["web"]]);
    expect(elk.edges?.[0]).toMatchObject({ sources: ["web"], targets: ["api"] });
    const laid = {
      ...elk,
      children: elk.children?.map((g, i) => ({
        ...g,
        x: i * 300,
        y: 0,
        width: 280,
        height: 120,
        children: g.children?.map((c) => ({ ...c, x: 20, y: 40 })),
      })),
    };
    const positioned = fromElk(laid, nodes);
    expect(positioned.groups).toHaveLength(2);
    expect(positioned.nodes.get("web")).toEqual({ x: 20, y: 40, groupId: groupOf(nodes[1]!).id });
  });

  it("labels types and edges", () => {
    expect(typeLabel("rds:db")).toBe("RDS database");
    expect(typeLabel("foo:some-thing")).toBe("some thing");
    expect(edgeLabel("deployed_by")).toBe("deployed by");
    expect(edgeLabel("new_kind")).toBe("new kind");
  });

  it("splits summary bullets and finds risks", () => {
    const lines = summaryLines("- api on ECS\n* db is private\n\n2. Risk: no stable endpoint");
    expect(lines).toEqual(["api on ECS", "db is private", "Risk: no stable endpoint"]);
    expect(lines.filter(isRiskLine).map(stripRisk)).toEqual(["no stable endpoint"]);
    expect(summaryLines(null)).toEqual([]);
  });
});
