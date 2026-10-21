import { describe, expect, it } from "vitest";

import { appOrigin, crossSiteRefusal } from "./csrf";

const APP = "https://groundkrew.com";
const h = (values: Record<string, string>) => new Headers(values);

describe("crossSiteRefusal", () => {
  it("lets reads through", () => {
    expect(crossSiteRefusal("GET", h({ origin: "https://evil.example", "sec-fetch-site": "cross-site" }), APP)).toBeNull();
  });

  it("accepts writes from this app's pages", () => {
    expect(crossSiteRefusal("POST", h({ origin: APP, "sec-fetch-site": "same-origin" }), APP)).toBeNull();
    expect(crossSiteRefusal("DELETE", h({ "sec-fetch-site": "same-origin" }), APP)).toBeNull();
  });

  it("refuses writes from anywhere else", () => {
    expect(crossSiteRefusal("POST", h({ origin: "https://evil.example" }), APP)).toMatch(/other sites/);
    expect(crossSiteRefusal("PATCH", h({ origin: APP, "sec-fetch-site": "same-site" }), APP)).toMatch(/other sites/);
    expect(crossSiteRefusal("PUT", h({ "sec-fetch-site": "cross-site" }), APP)).toMatch(/other sites/);
    expect(crossSiteRefusal("POST", h({}), APP)).toMatch(/no Origin/);
    expect(crossSiteRefusal("POST", h({ origin: "null" }), APP)).toMatch(/other sites/);
  });
});

describe("appOrigin", () => {
  it("prefers AUTH_URL", () => {
    expect(appOrigin("http://127.0.0.1:3000", "https://groundkrew.com/")).toBe(APP);
    expect(appOrigin("http://localhost:3000", undefined)).toBe("http://localhost:3000");
  });
});
