// @vitest-environment node
import { decodeProtectedHeader, jwtVerify } from "jose";
import { describe, expect, it } from "vitest";

import {
  INTERNAL_JWT_AUDIENCE,
  INTERNAL_JWT_ISSUER,
  INTERNAL_JWT_TTL_SECONDS,
  signInternalToken,
} from "./internal-jwt";

const SECRET = "test-internal-jwt-secret-0123456789abcdef";
const key = new TextEncoder().encode(SECRET);
const identity = {
  githubId: 1001,
  login: "alice",
  email: "alice@example.com",
  name: "Alice",
  avatarUrl: "https://avatars.example.com/1001",
};

describe("signInternalToken", () => {
  it("signs an HS256 token the API can verify, with the GitHub identity", async () => {
    const token = await signInternalToken(identity, SECRET);
    expect(decodeProtectedHeader(token).alg).toBe("HS256");
    const { payload } = await jwtVerify(token, key, {
      issuer: INTERNAL_JWT_ISSUER,
      audience: INTERNAL_JWT_AUDIENCE,
      algorithms: ["HS256"],
    });
    expect(payload).toMatchObject({
      sub: "1001",
      login: "alice",
      email: "alice@example.com",
      name: "Alice",
      avatar_url: "https://avatars.example.com/1001",
    });
  });

  it("expires after five minutes", async () => {
    const now = new Date("2026-10-06T12:00:00Z");
    const token = await signInternalToken(identity, SECRET, now);
    const { payload } = await jwtVerify(token, key, { currentDate: now });
    expect(payload.exp! - payload.iat!).toBe(INTERNAL_JWT_TTL_SECONDS);
    await expect(
      jwtVerify(token, key, { currentDate: new Date(now.getTime() + 6 * 60_000) }),
    ).rejects.toThrow(/exp/);
  });

  it("is rejected with any other secret", async () => {
    const token = await signInternalToken(identity, SECRET);
    await expect(
      jwtVerify(token, new TextEncoder().encode("some-other-secret-that-is-long-enough")),
    ).rejects.toThrow();
  });

  it("refuses a missing or short secret", async () => {
    await expect(signInternalToken(identity, undefined)).rejects.toThrow(/INTERNAL_JWT_SECRET/);
    await expect(signInternalToken(identity, "short")).rejects.toThrow(/INTERNAL_JWT_SECRET/);
  });

  it("refuses an incomplete identity", async () => {
    await expect(signInternalToken({ githubId: 0, login: "x" }, SECRET)).rejects.toThrow();
    await expect(signInternalToken({ githubId: 5, login: "" }, SECRET)).rejects.toThrow();
  });
});
