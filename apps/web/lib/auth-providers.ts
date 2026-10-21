/**
 * Which OAuth sign-ins this server offers: each one only when its credentials are set
 * (GitHub: AUTH_GITHUB_ID/SECRET, or the GitHub App's GITHUB_APP_CLIENT_ID/SECRET; Google:
 * AUTH_GOOGLE_ID/SECRET). Google is also off on a hosted instance (APP_ENV unset, staging, or
 * production) unless GOOGLE_AUTH_ENABLED=true, the same rule the API applies. Email and
 * password is offered only when the API says so (/auth/options).
 */
export function enabledOAuthProviders(env: Record<string, string | undefined> = process.env) {
  const local = env.APP_ENV === "dev" || env.APP_ENV === "test";
  const googleOn = env.GOOGLE_AUTH_ENABLED ? env.GOOGLE_AUTH_ENABLED === "true" : local;
  return {
    github: Boolean(
      (env.AUTH_GITHUB_ID || env.GITHUB_APP_CLIENT_ID) && (env.AUTH_GITHUB_SECRET || env.GITHUB_APP_CLIENT_SECRET),
    ),
    google: googleOn && Boolean(env.AUTH_GOOGLE_ID && env.AUTH_GOOGLE_SECRET),
  };
}

/**
 * The Groundcrew user id to keep in the session right after sign-in. For OAuth (GitHub,
 * Google) Auth.js replaces the id our `profile()` returned with a random UUID and keeps ours
 * only as `account.providerAccountId`; credentials sign-ins (email, e2e) keep `user.id`.
 */
export function signedInUserId(
  user: { id?: string | null } | undefined,
  account: { type: string; provider?: string; providerAccountId: string } | null | undefined,
): string | null {
  if (!account) return null;
  const id = account.type === "credentials" ? user?.id : account.providerAccountId;
  return id || null;
}
