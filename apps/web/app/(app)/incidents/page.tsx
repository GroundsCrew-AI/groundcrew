import type { Metadata } from "next";

import { IncidentsHome } from "./incidents-home";

export const metadata: Metadata = { title: "Incidents" };

export default async function IncidentsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { pending_fix } = await searchParams;
  return <IncidentsHome initialPendingFix={pending_fix === "true"} />;
}
