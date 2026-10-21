import { describe, expect, it } from "vitest";

import { contentSecurityPolicy, newNonce, securityHeaders } from "./security-headers";

describe("contentSecurityPolicy", () => {
  it("allows only this origin, the API, and the nonce's scripts", () => {
    const csp = contentSecurityPolicy({
      nonce: "abc",
      apiOrigin: "https://api.groundkrew.com/",
      oauthHosts: ["https://github.com"],
    });
    expect(csp).toContain("default-src 'self'");
    expect(csp).toContain("script-src 'self' 'nonce-abc' 'strict-dynamic';");
    expect(csp).toContain("connect-src 'self' https://api.groundkrew.com;");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toContain("object-src 'none'");
    expect(csp).toContain("form-action 'self' https://github.com");
    expect(csp).toMatch(/upgrade-insecure-requests$/);
    expect(csp).not.toContain("unsafe-eval");
  });

  it("lets the dev server reload", () => {
    const csp = contentSecurityPolicy({ nonce: "n", dev: true, apiOrigin: "http://localhost:8000" });
    expect(csp).toContain("'unsafe-eval'");
    expect(csp).toContain("connect-src 'self' http://localhost:8000 ws: wss:");
    expect(csp).not.toContain("upgrade-insecure-requests");
  });

  it("ignores an API URL it cannot parse", () => {
    expect(contentSecurityPolicy({ nonce: "n", apiOrigin: "not a url" })).toContain("connect-src 'self';");
  });
});

describe("securityHeaders", () => {
  it("sets HSTS only over HTTPS", () => {
    expect(securityHeaders({ https: true })["Strict-Transport-Security"]).toMatch(/max-age=63072000/);
    expect(securityHeaders({ https: false })["Strict-Transport-Security"]).toBeUndefined();
    expect(securityHeaders({ https: false })["X-Frame-Options"]).toBe("DENY");
  });

  it("makes a different nonce each time", () => {
    expect(newNonce()).not.toBe(newNonce());
    expect(newNonce()).toMatch(/^[A-Za-z0-9+/]{22}==$/);
  });
});
