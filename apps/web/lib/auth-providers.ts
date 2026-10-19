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
