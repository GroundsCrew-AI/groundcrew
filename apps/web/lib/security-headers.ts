/**
 * Security headers for every page the web app serves, set in middleware.ts.
 *
 * The Content-Security-Policy carries a fresh nonce per request: Next.js puts it on its own
 * scripts (it reads the policy from the request headers) and next-themes on its inline theme
 * script. Styles may be inline (Tailwind and Radix set style attributes). The browser talks to
 * this origin and to the API for live run updates (NEXT_PUBLIC_API_URL); nothing else.
 * frame-ancestors 'none' keeps every page out of other sites' frames.
 */

export type CspOptions = {
  nonce: string;
  /** The API origin the browser opens live streams on. */
  apiOrigin?: string;
  /** Next.js dev server: eval for fast refresh, websockets for HMR, plain http. */
  dev?: boolean;
  /** Sign-in providers whose pages a sign-in form redirects to. */
  oauthHosts?: string[];
};

function origin(url: string | undefined): string | undefined {
  if (!url) return undefined;
  try {
    return new URL(url).origin;
  } catch {
    return undefined;
  }
}

export function contentSecurityPolicy({ nonce, apiOrigin, dev = false, oauthHosts = [] }: CspOptions): string {
  const api = origin(apiOrigin);
  const directives: Record<string, string[]> = {
    "default-src": ["'self'"],
    "script-src": ["'self'", `'nonce-${nonce}'`, "'strict-dynamic'", ...(dev ? ["'unsafe-eval'"] : [])],
    "style-src": ["'self'", "'unsafe-inline'"],
    "img-src": ["'self'", "data:", "blob:", "https://avatars.githubusercontent.com", "https://lh3.googleusercontent.com"],
    "font-src": ["'self'", "data:"],
    "connect-src": ["'self'", ...(api ? [api] : []), ...(dev ? ["ws:", "wss:"] : [])],
    "frame-ancestors": ["'none'"],
    "frame-src": ["'none'"],
    "object-src": ["'none'"],
    "base-uri": ["'self'"],
    // Sign-in forms post here and are redirected to the provider.
    "form-action": ["'self'", ...oauthHosts],
    "manifest-src": ["'self'"],
    "worker-src": ["'self'", "blob:"],
  };
  const parts = Object.entries(directives).map(([name, values]) => `${name} ${values.join(" ")}`);
  if (!dev) parts.push("upgrade-insecure-requests");
  return parts.join("; ");
}

/** Headers on every response besides the CSP. HSTS only over HTTPS (nginx sets it too). */
export function securityHeaders({ https }: { https: boolean }): Record<string, string> {
  return {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), browsing-topics=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    ...(https ? { "Strict-Transport-Security": "max-age=63072000; includeSubDomains" } : {}),
  };
}

/** A random nonce, base64, from 16 bytes (Edge and Node both have Web Crypto). */
export function newNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return btoa(String.fromCharCode(...bytes));
}
