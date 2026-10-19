import { headers } from "next/headers";

import { signServiceToken } from "@/lib/internal-jwt";

/**
 * The API's sign-in endpoints (/auth), called from the web server only, with a service token.
 * The browser's address goes along for the API's rate limits.
 */
const API_BASE = (process.env.API_INTERNAL_URL || process.env.NEXT_PUBLIC_API_URL || "").replace(/\/+$/, "");

export type AuthUser = {
  id: string;
  email: string | null;
  email_verified: boolean;
  name: string | null;
  image: string | null;
};

export type AuthOptions = { password: boolean; require_verified_email: boolean; password_min_length: number };

export type AuthResult<T> = { ok: true; data: T } | { ok: false; status: number; code: string; detail: string };

export type OAuthProfile = {
  provider: "github" | "google";
  account_id: string;
  email: string | null;
  email_verified: boolean;
  login?: string | null;
  name?: string | null;
  avatar_url?: string | null;
};

async function clientIp(): Promise<string | undefined> {
  try {
    const h = await headers();
    return h.get("x-forwarded-for")?.split(",")[0]?.trim() || h.get("x-real-ip") || undefined;
  } catch {
    return undefined; // outside a request (build time)
  }
}

function failure(status: number, body: unknown): { ok: false; status: number; code: string; detail: string } {
  const b = (body ?? {}) as { detail?: unknown; code?: unknown };
  const detail =
    typeof b.detail === "string"
      ? b.detail
      : Array.isArray(b.detail)
        ? String((b.detail[0] as { msg?: string })?.msg ?? "Check the form and try again.").replace(/^Value error, /, "")
        : status >= 500
          ? "The server had a problem. Try again."
          : "Something went wrong. Try again.";
  return { ok: false, status, code: typeof b.code === "string" ? b.code : status === 422 ? "invalid_input" : "error", detail };
}

export async function authApi<T>(path: string, init: { method?: "GET" | "POST"; body?: unknown } = {}): Promise<AuthResult<T>> {
  if (!API_BASE) return { ok: false, status: 500, code: "config", detail: "API_INTERNAL_URL is not set." };
  let token: string;
  try {
    token = await signServiceToken(process.env.INTERNAL_JWT_SECRET);
  } catch (error) {
    return { ok: false, status: 500, code: "config", detail: error instanceof Error ? error.message : "Sign-in is not configured." };
  }
  const ip = await clientIp();
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/auth/${path}`, {
      method: init.method ?? "POST",
      headers: {
        authorization: `Bearer ${token}`,
        "content-type": "application/json",
        ...(ip ? { "x-client-ip": ip } : {}),
      },
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      cache: "no-store",
    });
  } catch {
    return { ok: false, status: 502, code: "unreachable", detail: "The API is not reachable. Try again in a moment." };
  }
  const body: unknown = await response.json().catch(() => undefined);
  if (!response.ok) return failure(response.status, body);
  return { ok: true, data: body as T };
}

/** What the sign-in page offers; email sign-in stays on when the API cannot be asked. */
export async function authOptions(): Promise<AuthOptions> {
  const result = await authApi<AuthOptions>("options", { method: "GET" });
  return result.ok ? result.data : { password: true, require_verified_email: true, password_min_length: 10 };
}
