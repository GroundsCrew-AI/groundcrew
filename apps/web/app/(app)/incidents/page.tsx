import type { Metadata } from "next";

import { IncidentsHome } from "./incidents-home";

export const metadata: Metadata = { title: "Incidents" };

export default function IncidentsPage() {
  return <IncidentsHome />;
}
