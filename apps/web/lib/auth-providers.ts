/**
 * Which OAuth sign-ins this server offers: each one only when its credentials are set
 * (GitHub: AUTH_GITHUB_ID/SECRET, or the GitHub App's GITHUB_APP_CLIENT_ID/SECRET; Google:
 * AUTH_GOOGLE_ID/SECRET). Email and password is always offered unless the API turns it off.
 */
export function enabledOAuthProviders(env: Record<string, string | undefined> = process.env) {
  return {
    github: Boolean(
      (env.AUTH_GITHUB_ID || env.GITHUB_APP_CLIENT_ID) && (env.AUTH_GITHUB_SECRET || env.GITHUB_APP_CLIENT_SECRET),
    ),
    google: Boolean(env.AUTH_GOOGLE_ID && env.AUTH_GOOGLE_SECRET),
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
