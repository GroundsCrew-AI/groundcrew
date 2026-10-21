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

/** Sign-in pages: open without a session (signed-in visitors are sent home, except sign-out). */
export const AUTH_PAGES: readonly string[] = [
  "/signin",
  "/signup",
  "/verify-email",
  "/forgot-password",
  "/reset-password",
  "/signout",
];

/** May this path be opened without a session? "/" is (signed-out visitors see the landing page). */
export function isOpenPath(pathname: string): boolean {
  return pathname === "/" || AUTH_PAGES.includes(pathname) || isPublicPath(pathname);
}
