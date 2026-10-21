import { describe, expect, it } from "vitest";

import { isPublicPath, PUBLIC_PATHS } from "./public-paths";

describe("isPublicPath", () => {
  it("lets the public pages and the template through", () => {
    for (const path of PUBLIC_PATHS) expect(isPublicPath(path), path).toBe(true);
    expect(isPublicPath("/docs/connect/aws")).toBe(true);
    expect(isPublicPath("/terms/")).toBe(true);
  });

  it("keeps everything else behind sign-in", () => {
    for (const path of ["/", "/settings", "/runs/123", "/terms-x", "/docsx", "/api/gc/me", "/welcome/../settings", "/docs/../settings", "/docs/./x"]) {
      expect(isPublicPath(path), path).toBe(false);
    }
  });
});
