// @vitest-environment node
import { decodeProtectedHeader, jwtVerify } from "jose";
import { describe, expect, it } from "vitest";

import {
  INTERNAL_JWT_AUDIENCE,
  INTERNAL_JWT_ISSUER,
  INTERNAL_JWT_TTL_SECONDS,
  SERVICE_JWT_AUDIENCE,
  signInternalToken,
  signServiceToken,
} from "./internal-jwt";

const SECRET = "test-internal-jwt-secret-0123456789abcdef";
const key = new TextEncoder().encode(SECRET);
const identity = { userId: "6f1c2b9e-1d2a-4c3b-9e8f-0a1b2c3d4e5f", authTime: 1_790_000_000 };

describe("signInternalToken", () => {
  it("signs an HS256 token the API can verify, with the user's id and sign-in time", async () => {
    const token = await signInternalToken(identity, SECRET);
    expect(decodeProtectedHeader(token).alg).toBe("HS256");
    const { payload } = await jwtVerify(token, key, {
      issuer: INTERNAL_JWT_ISSUER,
      audience: INTERNAL_JWT_AUDIENCE,
      algorithms: ["HS256"],
    });
    expect(payload).toMatchObject({ sub: identity.userId, auth_time: identity.authTime });
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

  it("refuses a session without a Groundcrew user (one from before accounts)", async () => {
    await expect(signInternalToken({ userId: "", authTime: 1 }, SECRET)).rejects.toThrow();
    await expect(signInternalToken({ userId: "990001", authTime: 1 }, SECRET)).rejects.toThrow();
    await expect(signInternalToken({ userId: identity.userId, authTime: Number.NaN }, SECRET)).rejects.toThrow();
  });
});

describe("signServiceToken", () => {
  it("is for the sign-in endpoints only, never a user's audience", async () => {
    const token = await signServiceToken(SECRET);
    const { payload } = await jwtVerify(token, key, { issuer: INTERNAL_JWT_ISSUER, audience: SERVICE_JWT_AUDIENCE });
    expect(payload.sub).toBe("web");
    expect(payload.exp! - payload.iat!).toBe(60);
    await expect(jwtVerify(token, key, { audience: INTERNAL_JWT_AUDIENCE })).rejects.toThrow();
  });
});
