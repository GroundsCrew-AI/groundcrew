/**
 * CSRF check for the /api/gc proxy. The session cookie is SameSite=Lax, so another site cannot
 * send it with a cross-site POST; this is the second line: a state-changing request must come
 * from this app's own pages. Browsers send Origin on every non-GET fetch and Sec-Fetch-Site on
 * every request, and a page cannot forge either.
 */

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export function crossSiteRefusal(
  method: string,
  headers: { get(name: string): string | null },
  appOrigin: string,
): string | null {
  if (SAFE_METHODS.has(method.toUpperCase())) return null;
  const site = headers.get("sec-fetch-site");
  if (site !== null && site !== "same-origin") return "Requests from other sites are refused.";
  const origin = headers.get("origin");
  if (origin === null) return site === "same-origin" ? null : "The request has no Origin header.";
  return origin === appOrigin ? null : "Requests from other sites are refused.";
}

/** This app's public origin: AUTH_URL's (what users open), else the request's own. */
export function appOrigin(requestOrigin: string, authUrl = process.env.AUTH_URL): string {
  if (authUrl) {
    try {
      return new URL(authUrl).origin;
    } catch {
      // Fall back to the request below.
    }
  }
  return requestOrigin;
}
