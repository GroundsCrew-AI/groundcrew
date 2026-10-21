/**
 * Pages and files anyone can open without signing in. The middleware lets these through; every
 * other page needs a session. "/docs" covers every page under it.
 */
export const PUBLIC_PATHS = ["/welcome", "/terms", "/privacy", "/security", "/docs", "/aws-connect.yaml"] as const;

/** True for a public path, or a page under /docs/. Exact otherwise ("/terms-x" is not public),
 * and never for a path with "." or ".." segments, which a server could resolve elsewhere. */
export function isPublicPath(pathname: string): boolean {
  if (pathname.split("/").some((part) => part === "." || part === "..")) return false;
  const path = pathname.length > 1 ? pathname.replace(/\/+$/, "") : pathname;
  return (PUBLIC_PATHS as readonly string[]).includes(path) || path.startsWith("/docs/");
}
