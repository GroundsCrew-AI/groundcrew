import { SignJWT } from "jose";

/** Must match apps/api/app/security/auth.py. */
export const INTERNAL_JWT_ISSUER = "groundcrew-web";
export const INTERNAL_JWT_AUDIENCE = "groundcrew-api";
export const INTERNAL_JWT_TTL_SECONDS = 5 * 60;
const MIN_SECRET_LENGTH = 32;

export type WebIdentity = {
  githubId: number;
  login: string;
  email?: string | null;
  name?: string | null;
  avatarUrl?: string | null;
};

/**
 * Sign the short-lived HS256 token the API trusts for one request. Server only: the secret
 * never reaches the browser.
 */
export async function signInternalToken(
  identity: WebIdentity,
  secret: string | undefined,
  now: Date = new Date(),
): Promise<string> {
  if (!secret || secret.length < MIN_SECRET_LENGTH) {
    throw new Error(`INTERNAL_JWT_SECRET must be set to at least ${MIN_SECRET_LENGTH} characters.`);
  }
  if (!Number.isInteger(identity.githubId) || identity.githubId <= 0 || !identity.login) {
    throw new Error("A signed-in GitHub identity is required.");
  }
  const issuedAt = Math.floor(now.getTime() / 1000);
  return new SignJWT({
    login: identity.login,
    email: identity.email ?? null,
    name: identity.name ?? null,
    avatar_url: identity.avatarUrl ?? null,
  })
    .setProtectedHeader({ alg: "HS256", typ: "JWT" })
    .setSubject(String(identity.githubId))
    .setIssuer(INTERNAL_JWT_ISSUER)
    .setAudience(INTERNAL_JWT_AUDIENCE)
    .setIssuedAt(issuedAt)
    .setExpirationTime(issuedAt + INTERNAL_JWT_TTL_SECONDS)
    .sign(new TextEncoder().encode(secret));
}
