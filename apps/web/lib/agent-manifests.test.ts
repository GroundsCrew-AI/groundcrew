import { describe, expect, it } from "vitest";

import { kubernetesApply, kubernetesConfig, kubernetesManifest, parseNamespaces } from "./agent-manifests";

const base = {
  token: "gcj_abc",
  apiUrl: "https://api.groundkrew.com",
  image: "groundkrew.com/public/groundcrew-agent:0.6.0",
  namespaces: ["default", "shop"],
};

describe("Kubernetes manifest", () => {
  it("reads only, in the namespaces named, unless asked to act", () => {
    const read = kubernetesManifest({ ...base, act: false });
    expect(read.match(/^kind: Role$/gm)).toHaveLength(2);
    expect(read.match(/^kind: RoleBinding$/gm)).toHaveLength(2);
    expect(read).toContain("namespace: shop");
    expect(read).toContain("verbs: [get, list, watch]");
    const verbs = read.split("\n").filter((l) => l.includes("verbs:"));
    expect(verbs.length).toBe(4);
    expect(verbs.every((l) => l.trim() === "verbs: [get, list, watch]")).toBe(true);
    expect(read).not.toContain("ClusterRole");

    const act = kubernetesManifest({ ...base, act: true });
    expect(act).toContain("verbs: [patch, create, delete]");
    expect(act).toContain("resources: [secrets]");
  });

  it("runs the image non-root and read-only, enrolling from the token Secret", () => {
    const m = kubernetesManifest({ ...base, act: false });
    expect(m).toContain("image: groundkrew.com/public/groundcrew-agent:0.6.0");
    expect(m).toContain("token: gcj_abc");
    expect(m).toContain("GROUNDCREW_JOIN_TOKEN_FILE");
    expect(m).toContain("runAsNonRoot: true");
    expect(m).toContain("readOnlyRootFilesystem: true");
    expect(m).toContain("capabilities: {drop: [ALL]}");
    expect(kubernetesApply({ ...base, act: false })).toMatch(/^kubectl apply -f - <<'EOF'\n[\s\S]*\nEOF$/);
  });

  it("writes a configuration with an example per namespace", () => {
    const c = kubernetesConfig(base.apiUrl, base.namespaces, true);
    expect(c).toContain("url: https://api.groundkrew.com");
    expect(c).toContain("services: []");
    expect(c).toContain("#   kubernetes: {namespace: shop, deployment: api}");
    expect(c).toContain("allow: [restart, rollback, deploy]");
  });

  it("takes namespace names only", () => {
    expect(parseNamespaces("shop, default  shop")).toEqual({ namespaces: ["default", "shop"], problems: [] });
    expect(parseNamespaces("Shop, ok").problems).toEqual(["Shop is not a namespace name"]);
  });
});
