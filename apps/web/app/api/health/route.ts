import { NextResponse } from "next/server";

/** Liveness for the container health check: the server answers. No session, no API call. */
export function GET() {
  return NextResponse.json({ status: "ok" });
}

export const dynamic = "force-dynamic";
