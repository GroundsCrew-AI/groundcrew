import { SignJWT } from "jose";

/** Must match apps/api/app/security/auth.py. */
export const INTERNAL_JWT_ISSUER = "groundcrew-web";
export const INTERNAL_JWT_AUDIENCE = "groundcrew-api";
export const SERVICE_JWT_AUDIENCE = "groundcrew-api-auth";
export const INTERNAL_JWT_TTL_SECONDS = 5 * 60;
export const SERVICE_JWT_TTL_SECONDS = 60;
const MIN_SECRET_LENGTH = 32;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The signed-in user as the API knows them: their Groundcrew id and when they signed in. */
export type WebIdentity = {
  userId: string;
  /** Seconds since the epoch. The API refuses sessions older than a password change. */
  authTime: number;
};

function key(secret: string | undefined): Uint8Array {
  if (!secret || secret.length < MIN_SECRET_LENGTH) {
    throw new Error(`INTERNAL_JWT_SECRET must be set to at least ${MIN_SECRET_LENGTH} characters.`);
  }
  return new TextEncoder().encode(secret);
}

/**
 * Sign the short-lived HS256 token the API trusts for one request. Server only: the secret
 * never reaches the browser.
 */
export async function signInternalToken(
  identity: WebIdentity,
  secret: string | undefined,
  now: Date = new Date(),
): Promise<string> {
  const signingKey = key(secret);
  if (!UUID.test(identity.userId) || !Number.isInteger(identity.authTime) || identity.authTime < 0) {
    throw new Error("A signed-in user is required.");
  }
  const issuedAt = Math.floor(now.getTime() / 1000);
  return new SignJWT({ auth_time: identity.authTime })
    .setProtectedHeader({ alg: "HS256", typ: "JWT" })
    .setSubject(identity.userId)
    .setIssuer(INTERNAL_JWT_ISSUER)
    .setAudience(INTERNAL_JWT_AUDIENCE)
    .setIssuedAt(issuedAt)
    .setExpirationTime(issuedAt + INTERNAL_JWT_TTL_SECONDS)
    .sign(signingKey);
}

/** The web server's own token for the API's sign-in endpoints (/auth). Server only. */
export async function signServiceToken(secret: string | undefined, now: Date = new Date()): Promise<string> {
  const signingKey = key(secret);
  const issuedAt = Math.floor(now.getTime() / 1000);
  return new SignJWT({})
    .setProtectedHeader({ alg: "HS256", typ: "JWT" })
    .setSubject("web")
    .setIssuer(INTERNAL_JWT_ISSUER)
    .setAudience(SERVICE_JWT_AUDIENCE)
    .setIssuedAt(issuedAt)
    .setExpirationTime(issuedAt + SERVICE_JWT_TTL_SECONDS)
    .sign(signingKey);
}
