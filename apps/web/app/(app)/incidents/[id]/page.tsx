import type { Metadata } from "next";

import { IncidentView } from "./incident-view";

export const metadata: Metadata = { title: "Incident" };

export default async function IncidentPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <IncidentView incidentId={id} />;
}
