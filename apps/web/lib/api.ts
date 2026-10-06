export const API_URL: string =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type Health = {
  status: string;
  version: string;
};

function isHealth(value: unknown): value is Health {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return typeof v.status === "string" && typeof v.version === "string";
}

export async function getHealth(signal?: AbortSignal): Promise<Health> {
  const res = await fetch(`${API_URL}/health`, { cache: "no-store", signal });
  if (!res.ok) throw new Error(`API returned ${res.status}`);
  const body: unknown = await res.json();
  if (!isHealth(body)) throw new Error("Unexpected response from API");
  return body;
}
