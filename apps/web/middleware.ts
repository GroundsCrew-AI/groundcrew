import { NextResponse } from "next/server";

import { auth } from "@/auth";
import { AUTH_PAGES, isOpenPath } from "@/lib/public-paths";
import { contentSecurityPolicy, newNonce, securityHeaders } from "@/lib/security-headers";

const OAUTH_HOSTS = ["https://github.com", "https://accounts.google.com"];

/**
 * Every page: sign-in where needed (auth.ts `authorized` lets public pages, the sign-in pages,
 * and "/" through), "/" shows the landing page to signed-out visitors, and each response gets
 * the security headers with a fresh CSP nonce (lib/security-headers.ts).
 */
export default auth((request) => {
  const { pathname } = request.nextUrl;
  const nonce = newNonce();
  const csp = contentSecurityPolicy({
    nonce,
    apiOrigin: process.env.NEXT_PUBLIC_API_URL,
    dev: process.env.NODE_ENV === "development",
    oauthHosts: OAUTH_HOSTS,
  });
  // Next.js reads the policy (and its nonce) from the request to tag its own scripts.
  const headers = new Headers(request.headers);
  headers.set("x-nonce", nonce);
  headers.set("content-security-policy", csp);

  const signedIn = Boolean(request.auth?.user?.id);
  let response: NextResponse;
  if (!signedIn && pathname === "/") {
    response = NextResponse.rewrite(new URL("/welcome", request.url), { request: { headers } });
  } else if (!signedIn && !isOpenPath(pathname)) {
    // With a middleware function Auth.js does not redirect on its own: send them to sign in
    // and back here afterwards.
    const signIn = new URL("/signin", request.url);
    signIn.searchParams.set("callbackUrl", pathname + request.nextUrl.search);
    response = NextResponse.redirect(signIn);
  } else if (signedIn && AUTH_PAGES.includes(pathname) && pathname !== "/signout") {
    response = NextResponse.redirect(new URL("/", request.url));
  } else {
    response = NextResponse.next({ request: { headers } });
  }
  response.headers.set("Content-Security-Policy", csp);
  const https = request.nextUrl.protocol === "https:" || request.headers.get("x-forwarded-proto") === "https";
  for (const [name, value] of Object.entries(securityHeaders({ https }))) response.headers.set(name, value);
  return response;
});

// Everything except Auth.js, the API proxy (they answer for themselves), the health check, and
// build assets.
export const config = {
  matcher: ["/((?!api/auth|api/gc|api/health|_next/static|_next/image|favicon.ico).*)"],
};
