import { describe, expect, it } from "vitest";

import { safeNext, signInMessage, signInPageNotice } from "./auth-forms";
import { enabledOAuthProviders } from "./auth-providers";

describe("safeNext", () => {
  it("keeps same-site paths with their query", () => {
    expect(safeNext("/incidents?status=open")).toBe("/incidents?status=open");
  });
  it.each(["https://evil.example/x", "//evil.example", "/\\evil.example", "", undefined, "/signin", "/signup?x=1"])(
    "turns %s into /",
    (next) => expect(safeNext(next)).toBe("/"),
  );
});

describe("sign-in messages", () => {
  it("explains each failure", () => {
    expect(signInMessage("unverified")).toMatch(/Confirm your email/);
    expect(signInMessage("locked")).toMatch(/reset your password/);
    expect(signInMessage("whatever")).toBe("Sign-in failed. Try again.");
  });
  it("turns the page's error and reason into a notice", () => {
    expect(signInPageNotice({ error: "CredentialsSignin", code: "invalid" })).toEqual({
      tone: "error",
      text: "Email or password is incorrect.",
    });
    expect(signInPageNotice({ reason: "password" })?.tone).toBe("info");
    expect(signInPageNotice({})).toBeNull();
  });
});

describe("enabledOAuthProviders", () => {
  it("offers a provider only with both its id and secret", () => {
    expect(enabledOAuthProviders({})).toEqual({ github: false, google: false });
    expect(enabledOAuthProviders({ AUTH_GOOGLE_ID: "id" }).google).toBe(false);
    expect(enabledOAuthProviders({ AUTH_GOOGLE_ID: "id", AUTH_GOOGLE_SECRET: "s" }).google).toBe(true);
    expect(enabledOAuthProviders({ GITHUB_APP_CLIENT_ID: "id", GITHUB_APP_CLIENT_SECRET: "s" }).github).toBe(true);
  });
});
