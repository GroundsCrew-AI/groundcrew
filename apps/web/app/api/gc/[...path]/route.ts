import { type NextRequest, NextResponse } from "next/server";

import { auth } from "@/auth";
import { clientIp } from "@/lib/auth-api";
import { appOrigin, crossSiteRefusal } from "@/lib/csrf";
import { signInternalToken } from "@/lib/internal-jwt";

/**
 * Server-side proxy from the browser to FastAPI. Every request gets a fresh 5-minute
 * internal JWT for the signed-in user, so the browser never holds an API credential and
 * REST needs no CORS. The live run stream does not go through here (see use-run-stream).
 */
const API_BASE = (process.env.API_INTERNAL_URL || process.env.NEXT_PUBLIC_API_URL || "").replace(
  /\/+$/,
  "",
);
const FORWARDED_RESPONSE_HEADERS = ["content-type", "x-request-id", "retry-after"];
// Same as the API's MAX_REQUEST_BYTES default; bigger bodies are refused before they are read.
const MAX_BODY_BYTES = 1_048_576;

type Context = { params: Promise<{ path: string[] }> };

async function proxy(request: NextRequest, context: Context): Promise<Response> {
  const refused = crossSiteRefusal(request.method, request.headers, appOrigin(request.nextUrl.origin));
  if (refused) return NextResponse.json({ detail: refused }, { status: 403 });
  const session = await auth();
  if (!session?.user?.id) {
    return NextResponse.json({ detail: "Sign in required." }, { status: 401 });
  }
  if (!API_BASE) {
    return NextResponse.json({ detail: "API_INTERNAL_URL is not set." }, { status: 500 });
  }
  const { path } = await context.params;
  // Sign-in endpoints take the web server's own token, never a user's: not proxied.
  if (path[0] === "auth") {
    return NextResponse.json({ detail: "Not found." }, { status: 404 });
  }

  let token: string;
  try {
    token = await signInternalToken(
      { userId: session.user.id, authTime: session.authAt },
      process.env.INTERNAL_JWT_SECRET,
    );
  } catch (error) {
    const detail = error instanceof Error ? error.message : "Could not sign the API token.";
    return NextResponse.json({ detail }, { status: 500 });
  }

  const target = `${API_BASE}/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`;
  const hasBody = !["GET", "HEAD"].includes(request.method);
  const length = Number(request.headers.get("content-length") ?? "0");
  if (hasBody && (!Number.isFinite(length) || length > MAX_BODY_BYTES)) {
    return NextResponse.json({ detail: "The request is too large." }, { status: 413 });
  }
  const headers: Record<string, string> = { authorization: `Bearer ${token}` };
  const contentType = request.headers.get("content-type");
  if (contentType) headers["content-type"] = contentType;
  // For per-address rate limits and the audit log.
  const ip = await clientIp();
  if (ip) headers["x-client-ip"] = ip;

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      body: hasBody ? await boundedText(request) : undefined,
      cache: "no-store",
    });
  } catch (error) {
    if (error instanceof BodyTooLarge) {
      return NextResponse.json({ detail: "The request is too large." }, { status: 413 });
    }
    return NextResponse.json({ detail: "The API is not reachable." }, { status: 502 });
  }

  const responseHeaders = new Headers();
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
}

/** The body as text, or a 413 error once it passes MAX_BODY_BYTES (chunked uploads too). */
async function boundedText(request: NextRequest): Promise<string> {
  const text = await request.text();
  if (new TextEncoder().encode(text).length > MAX_BODY_BYTES) throw new BodyTooLarge();
  return text;
}

class BodyTooLarge extends Error {}

export { proxy as DELETE, proxy as GET, proxy as PATCH, proxy as POST, proxy as PUT };
