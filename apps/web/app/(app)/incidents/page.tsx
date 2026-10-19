import type { Metadata } from "next";
import { Suspense } from "react";

import { RowsSkeleton } from "@/components/states/states";

import { IncidentsHome } from "./incidents-home";

export const metadata: Metadata = { title: "Incidents" };

export default function IncidentsPage() {
  // The list reads its filters from the URL (useSearchParams).
  return (
    <Suspense fallback={<RowsSkeleton rows={5} />}>
      <IncidentsHome />
    </Suspense>
  );
}
