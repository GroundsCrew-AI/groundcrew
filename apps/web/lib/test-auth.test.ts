import { afterEach, describe, expect, it, vi } from "vitest";

import { testAuthEnabled } from "./test-auth";

describe("dev-only test sign-in", () => {
  afterEach(() => vi.unstubAllEnvs());

  it.each([
    ["dev", "1", true],
    ["dev", undefined, false],
    ["production", "1", false],
    ["staging", "1", false],
    [undefined, "1", false],
  ])("APP_ENV=%s E2E_TEST_AUTH=%s -> %s", (appEnv, flag, expected) => {
    vi.stubEnv("APP_ENV", appEnv);
    vi.stubEnv("E2E_TEST_AUTH", flag);
    expect(testAuthEnabled()).toBe(expected);
  });
});
