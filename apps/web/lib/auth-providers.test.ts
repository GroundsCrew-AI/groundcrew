import { describe, expect, it } from "vitest";

import { enabledOAuthProviders, signedInUserId } from "./auth-providers";

const GROUNDCREW_ID = "a1c97fc5-fac3-4ed1-a602-04256ff3ec04";

describe("signedInUserId", () => {
  it("takes the Groundcrew id from the OAuth account, not Auth.js's random user id", () => {
    const user = { id: "5f0d3f4e-random-auth-js-uuid" };
    const account = { type: "oauth", provider: "github", providerAccountId: GROUNDCREW_ID };
    expect(signedInUserId(user, account)).toBe(GROUNDCREW_ID);
    expect(signedInUserId(user, { ...account, type: "oidc", provider: "google" })).toBe(GROUNDCREW_ID);
  });

  it("keeps the user id for email and password", () => {
    const account = { type: "credentials", provider: "password", providerAccountId: GROUNDCREW_ID };
    expect(signedInUserId({ id: GROUNDCREW_ID }, account)).toBe(GROUNDCREW_ID);
  });

  it("is null outside a sign-in", () => {
    expect(signedInUserId({ id: GROUNDCREW_ID }, null)).toBeNull();
    expect(signedInUserId(undefined, undefined)).toBeNull();
  });
});

describe("enabledOAuthProviders", () => {
  it("offers GitHub with the GitHub App's client credentials", () => {
    expect(enabledOAuthProviders({ GITHUB_APP_CLIENT_ID: "a", GITHUB_APP_CLIENT_SECRET: "b" })).toEqual({
      github: true,
      google: false,
    });
  });
});
