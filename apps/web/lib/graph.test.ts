import { describe, expect, it } from "vitest";

import { edge, node } from "@/tests/discovery-fixtures";

import {
  DEFAULT_FILTERS,
  edgeLabel,
  fromElk,
  groupOf,
  inlineParts,
  isRiskLine,
  labelWidth,
  stripRisk,
  summaryLines,
  TOOLING_GROUP,
  TOOLING_STUB,
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

  it("shows every resource, and IaC lines only when asked", () => {
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
    expect(shown.nodes.map((n) => n.id)).toEqual(["api", "sg", "repo", "wf"]);
    expect(shown.edges.map((e) => e.id)).toEqual(["e1", "e3"]);
    expect(visibleGraph(nodes, edges, { providers: [], showIac: true }).edges).toHaveLength(3);
    const awsOnly = visibleGraph(nodes, edges, { ...DEFAULT_FILTERS, providers: ["aws"] });
    expect(awsOnly.nodes.map((n) => n.id)).toEqual(["api", "sg"]);
  });

  it("folds tooling into one stub until it is opened, with sized edge labels", () => {
    const nodes = [
      node("api"),
      node("state", { type: "s3:bucket", name: "acme-tfstate", role: "tooling" }),
      node("tpl", { type: "s3:bucket", name: "groundcrew-templates-1", role: "tooling" }),
    ];
    const edges = [edge("e", "api", "state", { kind: "reads" })];
    expect(groupOf(nodes[1]!)).toEqual({ id: TOOLING_GROUP, label: "Tooling", provider: "tooling" });
    const closed = toElk(nodes, edges);
    const tooling = closed.children?.find((c) => c.id === TOOLING_GROUP);
    expect(tooling?.children?.map((c) => c.id)).toEqual([TOOLING_STUB]);
    expect(closed.edges?.[0]).toMatchObject({ sources: ["api"], targets: [TOOLING_STUB] });
    expect(closed.edges?.[0]?.labels?.[0]).toMatchObject({ text: "reads", width: labelWidth("reads") });
    const open = toElk(nodes, edges, true);
    expect(open.children?.find((c) => c.id === TOOLING_GROUP)?.children?.map((c) => c.id)).toEqual(["state", "tpl"]);
  });

  it("lays out Vercel, AWS, and GitHub side by side with routed edges and apart labels", async () => {
    const ELK = (await import("elkjs/lib/elk.bundled.js")).default;
    const nodes = [
      node("web", { provider: "vercel", type: "vercel:project", account: "team", region: null }),
      node("api"),
      node("worker"),
      node("db", { type: "rds:db" }),
      node("wf", { provider: "github", type: "github:workflow", account: null, region: null }),
    ];
    const edges = [
      edge("e1", "web", "api"),
      edge("e2", "api", "db", { kind: "reads" }),
      edge("e3", "worker", "db", { kind: "reads" }),
      edge("e4", "api", "wf", { kind: "deployed_by" }),
      edge("e5", "worker", "wf", { kind: "deployed_by" }),
      edge("e6", "web", "wf", { kind: "deployed_by" }),
    ];
    const positioned = fromElk(await new ELK().layout(toElk(nodes, edges)), nodes);
    const x = (provider: string) => positioned.groups.find((g) => g.group.provider === provider)!.x;
    expect(x("vercel")).toBeLessThan(x("aws"));
    expect(x("aws")).toBeLessThan(x("github"));
    const labels = [...positioned.edges.values()].map((r) => r.label!);
    expect(labels).toHaveLength(6);
    for (const route of positioned.edges.values()) expect(route.points.length).toBeGreaterThanOrEqual(2);
    // No two label boxes overlap.
    const boxes = [...positioned.edges.entries()].map(([id, r]) => {
      const w = labelWidth(edges.find((e) => e.id === id)!.kind.replace("_", " "));
      return { x: r.label!.x - w / 2, y: r.label!.y - 9, w, h: 18 };
    });
    for (let i = 0; i < boxes.length; i++)
      for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i]!;
        const b = boxes[j]!;
        const overlap = a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
        expect(overlap, `labels ${i} and ${j}`).toBe(false);
      }
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
    // The model writes markdown: bold and code become parts, stray markers go, and a bold
    // "Risk:" still counts as a risk line.
    expect(inlineParts("**checkout-api** reads `checkout/database-url` **")).toEqual([
      { text: "checkout-api", strong: true },
      { text: " reads " },
      { text: "checkout/database-url", code: true },
      { text: " " },
    ]);
    expect(inlineParts("<img src=x onerror=alert(1)>")).toEqual([{ text: "<img src=x onerror=alert(1)>" }]);
    const bold = "**Risk:** port 8000 is open";
    expect(isRiskLine(bold) && stripRisk(bold)).toBe("port 8000 is open");
  });
});
