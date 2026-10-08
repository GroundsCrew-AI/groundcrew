import { describe, expect, it } from "vitest";

import { configRows } from "./resource-summary";

describe("configRows", () => {
  it("summarizes an ECS service", () => {
    const rows = configRows("ecs:service", {
      running_count: 1,
      desired_count: 1,
      public_ips: ["3.15.10.20"],
      has_load_balancer: false,
      containers: [{ image_repository: "checkout-api", image_tag: "cef79e7a1b2c3d4e5f", ports: [{ port: 8000 }], secrets: [{ name: "DATABASE_URL" }] }],
    });
    const byLabel = Object.fromEntries(rows.map((r) => [r.label, r.value]));
    expect(byLabel.Tasks).toBe("1 running of 1");
    expect(byLabel.Images).toBe("checkout-api:cef79e7a1b2c");
    expect(byLabel["Load balancer"]).toBe("None");
    expect(byLabel["Secrets injected"]).toBe("DATABASE_URL");
  });

  it("says a secret's value is never read", () => {
    const rows = configRows("secretsmanager:secret", { rotation_enabled: false, version_stages: { "11111111": ["AWSCURRENT"] } });
    expect(rows[0]).toEqual({ label: "Value", value: "Never read by Groundcrew" });
    expect(rows.find((r) => r.label === "Versions")?.value).toBe("11111111 AWSCURRENT");
  });

  it("shows Vercel env names with types and only readable values", () => {
    const rows = configRows("vercel:project", {
      env: [{ key: "API_URL", type: "encrypted" }],
      env_values: { SITE_URL: "https://x.vercel.app" },
    });
    const byLabel = Object.fromEntries(rows.map((r) => [r.label, r.value]));
    expect(byLabel["Env vars"]).toBe("API_URL (encrypted)");
    expect(byLabel["Readable values"]).toBe("SITE_URL=https://x.vercel.app");
    expect(byLabel["Git repository"]).toBe("Not linked");
  });
});
