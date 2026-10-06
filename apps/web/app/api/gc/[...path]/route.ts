import { type NextRequest, NextResponse } from "next/server";

import { auth } from "@/auth";
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
const FORWARDED_RESPONSE_HEADERS = ["content-type", "x-request-id"];

type Context = { params: Promise<{ path: string[] }> };

async function proxy(request: NextRequest, context: Context): Promise<Response> {
  const session = await auth();
  if (!session?.user?.githubId) {
    return NextResponse.json({ detail: "Sign in required." }, { status: 401 });
  }
  if (!API_BASE) {
    return NextResponse.json({ detail: "API_INTERNAL_URL is not set." }, { status: 500 });
  }

  let token: string;
  try {
    token = await signInternalToken(
      {
        githubId: session.user.githubId,
        login: session.user.login,
        email: session.user.email,
        name: session.user.name,
        avatarUrl: session.user.avatarUrl,
      },
      process.env.INTERNAL_JWT_SECRET,
    );
  } catch (error) {
    const detail = error instanceof Error ? error.message : "Could not sign the API token.";
    return NextResponse.json({ detail }, { status: 500 });
  }

  const { path } = await context.params;
  const target = `${API_BASE}/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`;
  const hasBody = !["GET", "HEAD"].includes(request.method);
  const headers: Record<string, string> = { authorization: `Bearer ${token}` };
  const contentType = request.headers.get("content-type");
  if (contentType) headers["content-type"] = contentType;

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      body: hasBody ? await request.text() : undefined,
      cache: "no-store",
    });
  } catch {
    return NextResponse.json({ detail: "The API is not reachable." }, { status: 502 });
  }

  const responseHeaders = new Headers();
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
}

export { proxy as DELETE, proxy as GET, proxy as PATCH, proxy as POST, proxy as PUT };
